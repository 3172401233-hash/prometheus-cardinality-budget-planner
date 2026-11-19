import unittest

from planner import (
    ACTION_AGGREGATE,
    ACTION_DROP,
    ACTION_KEEP,
    ACTION_REDUCE_RETENTION,
    LabelSpec,
    MetricSpec,
    TenantQuota,
    plan,
)


def budget_of(result, tenant):
    for budget in result.budgets:
        if budget.tenant == tenant:
            return budget
    raise AssertionError("租户 %s 不在预算结果中" % tenant)


def decision_of(result, metric):
    for decision in result.decisions:
        if decision.metric == metric:
            return decision
    raise AssertionError("指标 %s 不在决策结果中" % metric)


class StableLowCardinalityTest(unittest.TestCase):
    def test_stable_metric_kept(self):
        metric = MetricSpec(
            name="http_requests_total",
            labels=[LabelSpec("method", 5), LabelSpec("status", 4)],
            tenant_shares={"team-a": 1.0},
            historical_series=(20, 20, 21, 20),
        )
        result = plan([metric], [TenantQuota("team-a", 100000)])
        decision = decision_of(result, "http_requests_total")
        self.assertEqual(decision.action, ACTION_KEEP)
        self.assertEqual(decision.estimated_savings_series, 0.0)
        self.assertFalse(decision.risks)
        budget = budget_of(result, "team-a")
        self.assertFalse(budget.over_budget)
        self.assertGreater(budget.remaining_series, 0)


class SingleHighCardinalityLabelTest(unittest.TestCase):
    def test_user_id_label_aggregated_when_over_budget(self):
        metric = MetricSpec(
            name="api_latency_seconds",
            labels=[
                LabelSpec("user_id", 50000, weekly_growth=1.5),
                LabelSpec("path", 200),
                LabelSpec("method", 5),
            ],
            tenant_shares={"team-a": 1.0},
            historical_series=(400000, 480000, 560000, 650000),
        )
        result = plan([metric], [TenantQuota("team-a", 100000)])
        decision = decision_of(result, "api_latency_seconds")
        self.assertEqual(decision.action, ACTION_AGGREGATE)
        self.assertIn("user_id", decision.dropped_labels)
        self.assertGreater(decision.estimated_savings_series, 0)
        self.assertGreater(decision.estimated_savings_bytes, 0)
        self.assertFalse(budget_of(result, "team-a").over_budget)

    def test_unprotected_metric_dropped_when_no_label_helps(self):
        metric = MetricSpec(
            name="debug_events_total",
            labels=[],
            tenant_shares={"team-a": 1.0},
            historical_series=(500,),
        )
        # 无标签可聚合且配额为 0，未受保护指标应被建议删除。
        result = plan([metric], [TenantQuota("team-a", 0)])
        decision = decision_of(result, "debug_events_total")
        self.assertEqual(decision.action, ACTION_DROP)
        self.assertEqual(decision.projected_series, 0.0)


class SharedMetricTest(unittest.TestCase):
    def test_shared_label_cost_attributed_per_tenant(self):
        shared = MetricSpec(
            name="shared_requests_total",
            labels=[LabelSpec("user_id", 10000)],
            tenant_shares={"team-a": 3.0, "team-b": 1.0},
            historical_series=(10000,),
        )
        result = plan(
            [shared],
            [TenantQuota("team-a", 1000000), TenantQuota("team-b", 1000000)],
        )
        decision = decision_of(result, "shared_requests_total")
        self.assertAlmostEqual(decision.tenant_costs["team-a"], 10000 * 0.75)
        self.assertAlmostEqual(decision.tenant_costs["team-b"], 10000 * 0.25)
        self.assertAlmostEqual(
            decision.tenant_costs["team-a"] + decision.tenant_costs["team-b"],
            decision.projected_series,
        )
        self.assertAlmostEqual(budget_of(result, "team-a").used_series, 7500.0)
        self.assertAlmostEqual(budget_of(result, "team-b").used_series, 2500.0)

    def test_shared_metric_remediation_relieves_all_tenants(self):
        shared = MetricSpec(
            name="shared_latency",
            labels=[LabelSpec("user_id", 20000), LabelSpec("path", 100)],
            tenant_shares={"team-a": 1.0, "team-b": 1.0},
        )
        result = plan(
            [shared],
            [TenantQuota("team-a", 50000), TenantQuota("team-b", 50000)],
        )
        decision = decision_of(result, "shared_latency")
        self.assertEqual(decision.action, ACTION_AGGREGATE)
        self.assertFalse(budget_of(result, "team-a").over_budget)
        self.assertFalse(budget_of(result, "team-b").over_budget)


class SLOProtectionTest(unittest.TestCase):
    def test_slo_metric_never_dropped_and_referenced_label_kept(self):
        metric = MetricSpec(
            name="slo_availability_ratio",
            labels=[
                LabelSpec("service", 10, referenced=True),
                LabelSpec("user_id", 8000, weekly_growth=1.2),
            ],
            tenant_shares={"team-a": 1.0},
            referenced_by=("slo:api-availability", "alert:HighErrorRate"),
            historical_series=(80000,),
        )
        result = plan([metric], [TenantQuota("team-a", 1000)])
        decision = decision_of(result, "slo_availability_ratio")
        self.assertNotEqual(decision.action, ACTION_DROP)
        self.assertNotIn("service", decision.dropped_labels)
        self.assertIn(decision.action, (ACTION_AGGREGATE, ACTION_REDUCE_RETENTION))

    def test_protected_metric_without_droppable_labels_reduces_retention(self):
        metric = MetricSpec(
            name="slo_latency",
            labels=[LabelSpec("service", 50, referenced=True)],
            tenant_shares={"team-a": 1.0},
            referenced_by=("slo:latency",),
            retention_days=30.0,
            historical_series=(50,),
        )
        result = plan([metric], [TenantQuota("team-a", 10)])
        decision = decision_of(result, "slo_latency")
        self.assertEqual(decision.action, ACTION_REDUCE_RETENTION)
        self.assertIsNotNone(decision.new_retention_days)
        self.assertLess(decision.new_retention_days, 30.0)
        self.assertGreaterEqual(decision.new_retention_days, 1.0)
        # 序列数无法下降，预算不足必须被明确报告。
        self.assertTrue(budget_of(result, "team-a").over_budget)
        self.assertTrue(any("预算不足" in risk for risk in result.risks))


class BudgetBoundaryTest(unittest.TestCase):
    def test_usage_exactly_at_quota_kept(self):
        metric = MetricSpec(
            name="boundary_metric",
            labels=[LabelSpec("code", 100)],
            tenant_shares={"team-a": 1.0},
            historical_series=(100,),
        )
        result = plan([metric], [TenantQuota("team-a", 100)])
        decision = decision_of(result, "boundary_metric")
        self.assertEqual(decision.action, ACTION_KEEP)
        budget = budget_of(result, "team-a")
        self.assertEqual(budget.remaining_series, 0.0)
        self.assertFalse(budget.over_budget)

    def test_usage_one_over_quota_triggers_remediation(self):
        metric = MetricSpec(
            name="boundary_metric",
            labels=[LabelSpec("code", 101), LabelSpec("extra", 2)],
            tenant_shares={"team-a": 1.0},
        )
        result = plan([metric], [TenantQuota("team-a", 201)])
        decision = decision_of(result, "boundary_metric")
        # 101 * 2 = 202 > 201，聚合 extra 后回落到 101。
        self.assertEqual(decision.action, ACTION_AGGREGATE)
        self.assertFalse(budget_of(result, "team-a").over_budget)


class GrowthSpikeTest(unittest.TestCase):
    def test_anomalous_label_growth_aggregated_even_within_budget(self):
        metric = MetricSpec(
            name="spiky_metric",
            labels=[
                LabelSpec("endpoint", 10),
                LabelSpec("request_id", 100, weekly_growth=30.0),
            ],
            tenant_shares={"team-a": 1.0},
        )
        result = plan([metric], [TenantQuota("team-a", 10 ** 12)])
        decision = decision_of(result, "spiky_metric")
        self.assertEqual(decision.action, ACTION_AGGREGATE)
        self.assertIn("request_id", decision.dropped_labels)
        self.assertTrue(any("增长异常" in risk for risk in decision.risks))

    def test_history_spike_flagged_as_risk(self):
        metric = MetricSpec(
            name="history_spike",
            labels=[LabelSpec("path", 50)],
            tenant_shares={"team-a": 1.0},
            historical_series=(50, 55, 52, 800),
        )
        result = plan([metric], [TenantQuota("team-a", 10 ** 9)])
        decision = decision_of(result, "history_spike")
        self.assertTrue(any("突增" in risk for risk in decision.risks))

    def test_missing_history_flagged_and_estimated(self):
        metric = MetricSpec(
            name="no_history",
            labels=[LabelSpec("path", 20), LabelSpec("method", 5)],
            tenant_shares={"team-a": 1.0},
        )
        result = plan([metric], [TenantQuota("team-a", 10 ** 9)])
        decision = decision_of(result, "no_history")
        self.assertEqual(decision.current_series, 100.0)
        self.assertTrue(any("历史数据缺失" in risk for risk in decision.risks))

    def test_projection_uses_growth_not_current_cardinality(self):
        metric = MetricSpec(
            name="growing",
            labels=[LabelSpec("tenant_id", 100, weekly_growth=1.5)],
            tenant_shares={"team-a": 1.0},
        )
        result = plan([metric], [TenantQuota("team-a", 10 ** 9)], horizon_weeks=4)
        decision = decision_of(result, "growing")
        # 100 * 1.5^4 = 506.25，而不是当前的 100。
        self.assertAlmostEqual(decision.projected_series, 506.25)


if __name__ == "__main__":
    unittest.main()
