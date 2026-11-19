# prometheus-cardinality-budget-planner
按租户和指标生命周期规划 Prometheus 标签基数预算

## 背景

服务给指标增加用户 ID、请求路径等标签后，活跃序列数量可能在一周内增长几十倍。
直接删除高基数指标又会一起丢掉排障和 SLO 依赖的数据。本规划器根据标签值的
增长趋势推算未来序列占用，而不是只按当前基数做判断，并给出可执行的处置建议。

## 功能

- 输入指标族、标签集合（含各标签值的周增长系数）、租户配额、抓取频率、
  保留时间、SLO/告警引用和历史序列数量，推算规划周期内每个指标的
  序列占用与存储预算（`series × retention / scrape_interval × 每样本字节数`）。
- 对每个指标输出四种决策之一：`keep`（保留原样）、`aggregate_labels`
  （聚合高基数标签）、`reduce_retention`（缩短保留时间）、`drop`（删除）。
- 被告警或 SLO 引用的指标永远不会被建议删除；被引用的标签永远不会被聚合。
- 多租户共享高基数标签时，按 `tenant_shares` 比例把序列成本归因到各租户。
- 明确处理异常情况：新标签增长异常（周增长系数 ≥ 2 时主动聚合未引用标签）、
  历史数据缺失（回退到标签基数估算并标记风险）、历史序列突增、预算不足
  （执行全部建议后仍超配额时输出全局风险）。
- 输出每个指标的决策、预计节省（序列数与存储字节）、各租户剩余预算和风险原因。

## 使用

```python
from planner import LabelSpec, MetricSpec, TenantQuota, plan

metrics = [
    MetricSpec(
        name="api_latency_seconds",
        labels=[
            LabelSpec("user_id", 50000, weekly_growth=1.5),
            LabelSpec("path", 200),
        ],
        tenant_shares={"team-a": 1.0},
        scrape_interval_seconds=15,
        retention_days=15,
        referenced_by=("slo:api-availability",),
        historical_series=(400000, 480000, 560000, 650000),
    ),
]
result = plan(metrics, [TenantQuota("team-a", 100000)], horizon_weeks=4)
for decision in result.decisions:
    print(decision.metric, decision.action, decision.dropped_labels, decision.risks)
```

## 测试

```bash
python -m unittest
```

兼容 Python 3.8+。

## 重要说明

本规划器**只做容量规划与决策建议，不直接修改 Prometheus 配置**。输出的
`drop`、`aggregate_labels`、`reduce_retention` 均为建议，需要由运维人员
评审后通过 relabel 规则、recording rules 或保留参数变更等方式手动落地，
避免误删排障和 SLO 依赖的数据。
