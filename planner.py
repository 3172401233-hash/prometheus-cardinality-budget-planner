"""Prometheus 基数预算规划器。

根据指标族、标签集合、标签值增长、租户配额、抓取频率、保留时间、
SLO/告警引用和历史序列数量，推算规划周期内每个指标的序列占用与
存储预算，并给出 keep / aggregate_labels / reduce_retention / drop
决策。本模块只做规划，不修改任何 Prometheus 配置。
"""

from dataclasses import dataclass, field

BYTES_PER_SAMPLE = 2.0
SECONDS_PER_DAY = 86400.0
GROWTH_SPIKE_THRESHOLD = 2.0
HISTORY_SPIKE_RATIO = 3.0
MIN_RETENTION_DAYS = 1.0

ACTION_KEEP = "keep"
ACTION_AGGREGATE = "aggregate_labels"
ACTION_REDUCE_RETENTION = "reduce_retention"
ACTION_DROP = "drop"


@dataclass
class LabelSpec:
    name: str
    current_values: int
    weekly_growth: float = 1.0
    referenced: bool = False  # 被告警 / SLO 查询引用，不可聚合掉


@dataclass
class MetricSpec:
    name: str
    labels: list = field(default_factory=list)
    tenant_shares: dict = field(default_factory=dict)  # tenant -> 成本归因比例
    scrape_interval_seconds: float = 15.0
    retention_days: float = 15.0
    referenced_by: tuple = ()          # 例如 ("alert:HighLatency", "slo:api-avail")
    historical_series: tuple = ()      # 按周排列的历史序列数量，最旧 -> 最新

    @property
    def protected(self):
        return bool(self.referenced_by)


@dataclass
class TenantQuota:
    tenant: str
    max_series: int


@dataclass
class MetricDecision:
    metric: str
    action: str
    current_series: float
    projected_series: float           # 决策生效后的预计序列数
    projected_bytes: float
    estimated_savings_series: float   # 相对“全部保持原样”基线
    estimated_savings_bytes: float
    dropped_labels: tuple = ()
    new_retention_days: float = None
    tenant_costs: dict = field(default_factory=dict)  # tenant -> 归因序列数
    risks: list = field(default_factory=list)
    reasons: list = field(default_factory=list)


@dataclass
class TenantBudget:
    tenant: str
    max_series: int
    used_series: float
    remaining_series: float
    over_budget: bool


@dataclass
class PlanResult:
    decisions: list
    budgets: list
    risks: list


def estimate_current_series(metric):
    """优先使用最近一期历史数据；缺失时由标签基数乘积估算。"""
    if metric.historical_series:
        return float(metric.historical_series[-1])
    series = 1.0
    for label in metric.labels:
        series *= max(1, label.current_values)
    return series


def detect_history_spike(history):
    """最近一期相对此前中位数的突增检测。"""
    if len(history) < 2:
        return False
    baseline = sorted(history[:-1])[len(history[:-1]) // 2]
    if baseline <= 0:
        return history[-1] > 0
    return history[-1] / baseline >= HISTORY_SPIKE_RATIO


def project_label_values(label, weeks):
    return max(1.0, label.current_values * (label.weekly_growth ** weeks))


def project_series(metric, weeks, dropped_labels=()):
    """按各标签值的增长推算未来序列数，而不是只看当前基数。"""
    dropped = set(dropped_labels)
    if metric.historical_series and not dropped:
        # 有历史数据时以实际观测为基准，再叠加标签增长系数。
        growth_factor = 1.0
        for label in metric.labels:
            growth_factor *= label.weekly_growth ** weeks
        return estimate_current_series(metric) * growth_factor
    series = 1.0
    for label in metric.labels:
        if label.name in dropped:
            continue
        series *= project_label_values(label, weeks)
    return series


def storage_bytes(series, retention_days, scrape_interval_seconds):
    samples = retention_days * SECONDS_PER_DAY / max(1.0, scrape_interval_seconds)
    return series * samples * BYTES_PER_SAMPLE


def _normalize_shares(metric):
    if not metric.tenant_shares:
        return {"default": 1.0}
    total = sum(metric.tenant_shares.values())
    if total <= 0:
        return {tenant: 1.0 / len(metric.tenant_shares) for tenant in metric.tenant_shares}
    return {tenant: share / total for tenant, share in metric.tenant_shares.items()}


def attribute_costs(metric, series):
    """多租户共享高基数标签时，按份额把序列成本归因到各租户。"""
    return {tenant: series * share for tenant, share in _normalize_shares(metric).items()}


def _anomalous_labels(metric):
    return [l for l in metric.labels if l.weekly_growth >= GROWTH_SPIKE_THRESHOLD]


def _aggregation_candidates(metric):
    # 只能聚合未被告警 / SLO 引用的标签，优先处理增长快、基数高的。
    candidates = [l for l in metric.labels if not l.referenced]
    candidates.sort(key=lambda l: (l.weekly_growth, l.current_values), reverse=True)
    return candidates


def _build_risks(metric):
    risks = []
    if not metric.historical_series:
        risks.append("历史数据缺失：当前序列数由标签基数估算，存在偏差风险")
    elif detect_history_spike(metric.historical_series):
        risks.append("历史序列数量出现异常突增")
    for label in _anomalous_labels(metric):
        risks.append("标签 %s 增长异常（周增长系数 %.2f）" % (label.name, label.weekly_growth))
    return risks


def plan(metrics, quotas, horizon_weeks=4):
    """生成基数预算规划。

    metrics: MetricSpec 列表；quotas: TenantQuota 列表；
    horizon_weeks: 规划周期（周）。
    """
    quota_map = {q.tenant: q.max_series for q in quotas}
    decisions = []
    global_risks = []

    # 基线：全部保持原样时的占用，用于计算节省。
    baseline_series = {}
    for metric in metrics:
        baseline_series[metric.name] = project_series(metric, horizon_weeks)

    # 第一轮：默认 keep，但对增长异常且未受保护的标签主动聚合。
    planned = {}
    for metric in metrics:
        risks = _build_risks(metric)
        dropped = []
        reasons = []
        action = ACTION_KEEP
        anomalous = [l for l in _anomalous_labels(metric) if not l.referenced]
        if anomalous:
            action = ACTION_AGGREGATE
            dropped = [l.name for l in anomalous]
            reasons.append("标签 %s 增长异常，主动聚合以控制未来基数"
                           % "、".join(l.name for l in anomalous))
            for l in _anomalous_labels(metric):
                if l.referenced:
                    risks.append("异常增长标签 %s 被告警/SLO 引用，无法聚合" % l.name)
        planned[metric.name] = {
            "action": action,
            "dropped": dropped,
            "retention_days": metric.retention_days,
            "risks": risks,
            "reasons": reasons,
        }

    def planned_series(metric):
        p = planned[metric.name]
        if p["action"] == ACTION_DROP:
            return 0.0
        return project_series(metric, horizon_weeks, p["dropped"])

    def tenant_usage():
        usage = {}
        for metric in metrics:
            for tenant, cost in attribute_costs(metric, planned_series(metric)).items():
                usage[tenant] = usage.get(tenant, 0.0) + cost
        return usage

    # 第二轮：对超配额的租户，按预计占用从大到小依次升级处置。
    usage = tenant_usage()
    over_tenants = {t for t, used in usage.items()
                    if t in quota_map and used > quota_map[t]}
    if over_tenants:
        order = sorted(metrics, key=lambda m: planned_series(m), reverse=True)
        for metric in order:
            shares = _normalize_shares(metric)
            if not any(t in over_tenants for t in shares):
                continue
            p = planned[metric.name]
            if p["action"] == ACTION_DROP:
                continue
            # 1) 尝试聚合更多未引用标签
            remaining = [l for l in _aggregation_candidates(metric)
                         if l.name not in p["dropped"]]
            if remaining and p["action"] != ACTION_REDUCE_RETENTION:
                p["action"] = ACTION_AGGREGATE
                p["dropped"] = p["dropped"] + [l.name for l in remaining]
                p["reasons"].append("租户配额不足，聚合全部未引用高基数标签")
            else:
                # 2) 无标签可聚合：未保护指标直接 drop，受保护指标缩短保留时间
                if metric.protected:
                    # 保留时间只影响存储占用，不减少序列数；
                    # 按超出比例缩短保留时间以释放存储预算。
                    p["action"] = ACTION_REDUCE_RETENTION
                    usage_now = tenant_usage()
                    worst_ratio = 1.0
                    for tenant in shares:
                        if tenant not in quota_map or quota_map[tenant] <= 0:
                            continue
                        ratio = usage_now.get(tenant, 0.0) / quota_map[tenant]
                        worst_ratio = max(worst_ratio, ratio)
                    new_retention = max(MIN_RETENTION_DAYS,
                                        metric.retention_days / worst_ratio)
                    p["retention_days"] = min(p["retention_days"], new_retention)
                    p["reasons"].append(
                        "指标被告警/SLO 引用，不可删除或聚合引用标签，缩短保留时间至 %.1f 天"
                        % p["retention_days"])
                    p["risks"].append("保留时间缩短，长期排障与趋势分析能力受限")
                else:
                    p["action"] = ACTION_DROP
                    p["reasons"].append("租户配额不足且指标未被告警/SLO 引用，建议删除")
                    p["risks"].append("删除后不可恢复，需确认无隐性看板依赖")
            usage = tenant_usage()
            over_tenants = {t for t, used in usage.items()
                            if t in quota_map and used > quota_map[t]}
            if not over_tenants:
                break

    # 汇总决策与预算。
    final_usage = tenant_usage()
    for metric in metrics:
        p = planned[metric.name]
        series = planned_series(metric)
        retention = p["retention_days"]
        base = baseline_series[metric.name]
        saved_series = max(0.0, base - series)
        decisions.append(MetricDecision(
            metric=metric.name,
            action=p["action"],
            current_series=estimate_current_series(metric),
            projected_series=series,
            projected_bytes=storage_bytes(series, retention, metric.scrape_interval_seconds),
            estimated_savings_series=saved_series,
            estimated_savings_bytes=storage_bytes(saved_series, retention,
                                                  metric.scrape_interval_seconds),
            dropped_labels=tuple(p["dropped"]),
            new_retention_days=(retention if p["action"] == ACTION_REDUCE_RETENTION else None),
            tenant_costs=attribute_costs(metric, series),
            risks=p["risks"],
            reasons=p["reasons"],
        ))

    budgets = []
    tenants = sorted(set(quota_map) | set(final_usage))
    for tenant in tenants:
        used = final_usage.get(tenant, 0.0)
        if tenant not in quota_map:
            budgets.append(TenantBudget(tenant, 0, used, -used, True))
            global_risks.append("租户 %s 未配置配额，无法约束其基数" % tenant)
            continue
        remaining = quota_map[tenant] - used
        over = remaining < 0
        budgets.append(TenantBudget(tenant, quota_map[tenant], used, remaining, over))
        if over:
            global_risks.append(
                "租户 %s 预算不足：即使执行全部建议仍超出 %.0f 个序列，"
                "需要提高配额或进一步缩减指标范围" % (tenant, -remaining))

    return PlanResult(decisions=decisions, budgets=budgets, risks=global_risks)
