"""Costs (commission and slippage) in sizing, the risk check and the Time Machine.

The reel's trade: TSLA entry 364.18, stop 358.32 (5.86 a share), target 370.04,
$50 max loss -> 8 shares. With no costs set every number stays the reel's."""

import gzip
import json
import os
import unittest

from _load import load, ROOT, FIX

size = load("trader-budget", "size")
check = load("trader-risk", "check")
tm = load("trader-timemachine", "timemachine")

CFG = os.path.join(ROOT, "trader-budget", "config.example.json")
PLAN = os.path.join(FIX, "trader-budget", "plan-tsla.json")
ORDERS = os.path.join(FIX, "trader-risk", "orders-tsla.json")
with gzip.open(os.path.join(FIX, "trader-timemachine", "TSLA-60d5m.json.gz"), "rt", encoding="utf-8") as _fh:
    CHART = _fh.read()
TODAY = "2026-10-19"


def cfg(**over):
    with open(CFG) as fh:
        c = json.load(fh)
    c.update(over)
    return c


def plan():
    with open(PLAN) as fh:
        return json.load(fh)


def tsla():
    with open(ORDERS) as fh:
        return json.load(fh)["orders"][0]


class Sizing(unittest.TestCase):
    def test_no_costs_keeps_the_reel_and_says_so(self):
        s = size.size_trade(plan(), cfg(), today=TODAY)
        self.assertEqual((s["qty"], s["max_loss"], s["costs_at_stop"]), (8, 46.88, 0.0))
        self.assertIn("Costs: none set", size.plan_report(s, size.bracket_order(s)))

    def test_costs_go_into_the_max_loss_and_reward_risk(self):
        s = size.size_trade(plan(), cfg(commission_per_order_usd=1, commission_per_share_usd=0.005,
                                        slippage_per_share=0.05, min_reward_risk=0), today=TODAY)
        # (50 - 2 commissions) / (5.86 + 2 x 0.05 slippage + 2 x 0.005 commission) = 8.04 -> 8
        self.assertEqual(s["qty"], 8)
        self.assertEqual(s["costs_at_stop"], 2.88)          # 2 + 8 x 0.01 + 8 x 0.10
        self.assertEqual(s["costs_at_target"], 2.48)        # 2 + 8 x 0.01 + 8 x 0.05
        self.assertEqual(s["max_loss"], 49.76)
        self.assertEqual(s["max_loss_before_costs"], 46.88)
        self.assertEqual(s["reward_risk_net"], 0.89)        # (46.88 - 2.48) / 49.76
        text = size.plan_report(s, size.bracket_order(s))
        self.assertIn("plus $2.88 in costs = $49.76", text)
        self.assertIn("$49.76 with costs", size.bracket_order(s)["why"])

    def test_slippage_cuts_the_size(self):
        s = size.size_trade(plan(), cfg(slippage_per_share=0.5, min_reward_risk=0), today=TODAY)
        self.assertEqual(s["qty"], 7)                       # 50 / 6.86 = 7.29
        self.assertLessEqual(s["max_loss"], 50)

    def test_reward_risk_after_costs_can_refuse(self):
        s = size.size_trade(plan(), cfg(slippage_per_share=0.05), today=TODAY)
        self.assertIsNone(size.bracket_order(s))
        self.assertIn("after costs", s["refusal"])

    def test_commissions_bigger_than_the_max_loss(self):
        s = size.size_trade(plan(), cfg(commission_per_order_usd=25), today=TODAY)
        self.assertEqual(s["qty"], 0)
        self.assertIn("in costs", s["refusal"])

    def test_negative_costs_are_refused(self):
        with self.assertRaises(size.SizeError):
            size.size_trade(plan(), cfg(slippage_per_share=-0.01), today=TODAY)


class RiskCheck(unittest.TestCase):
    NOW = check.parse_now("2026-10-19 10:15")

    def one(self, **over):
        res = check.check([tsla()], cfg(**over), now=self.NOW)
        return res, " | ".join(res["results"][0]["reasons"])

    def test_no_costs_passes_the_reel_and_says_so(self):
        res, _ = self.one()
        self.assertEqual(res["results"][0]["verdict"], "PASS")
        self.assertIn("Costs: none set", check.report(res))

    def test_costs_can_push_the_loss_over_the_limit(self):
        res, why = self.one(slippage_per_share=0.5, min_reward_risk=0)
        self.assertEqual(res["results"][0]["verdict"], "BLOCK")
        self.assertIn("+ $8 costs = $54.88 at the stop", why)
        self.assertNotIn("Costs: none set", check.report(res))

    def test_reward_risk_after_costs(self):
        res, why = self.one(slippage_per_share=0.05)
        self.assertEqual(res["results"][0]["verdict"], "BLOCK")
        self.assertIn("0.97 after costs", why)               # (46.88 - 0.40) / 47.68

    def test_costs_count_in_the_logged_max_loss(self):
        res, _ = self.one(commission_per_order_usd=1, min_reward_risk=0)
        self.assertEqual(res["results"][0]["max_loss"], 48.88)

    def test_budget_and_risk_agree(self):
        c = cfg(commission_per_order_usd=1, commission_per_share_usd=0.005, slippage_per_share=0.05,
                min_reward_risk=0)
        s = size.size_trade(plan(), c, today=TODAY)
        res = check.check([size.bracket_order(s)], c, now=self.NOW)
        self.assertEqual(res["results"][0]["max_loss"], s["max_loss"])
        self.assertEqual(res["results"][0]["verdict"], "PASS")


class BrokerTicket(unittest.TestCase):
    def test_the_ticket_shows_the_loss_with_costs(self):
        ticket = load("trader-broker", "ticket")
        c = cfg(commission_per_order_usd=1, commission_per_share_usd=0.005, slippage_per_share=0.05,
                min_reward_risk=0)
        for limit in (None, 364.18):
            s = size.size_trade(plan(), c, limit=limit, today=TODAY)
            self.assertEqual(ticket.max_loss(size.bracket_order(s))[0], 49.76, limit)


class TimeMachine(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        raw = plan()
        raw.update({"entry_bar": 8, "interval": "5m", "risk_per_share": 5.86, "reward_risk": 1.0})
        cls.plan = tm.normalize_plan(raw)
        cls.now = tm.parse_now("2026-10-19 10:50")
        cls.free = tm.time_machine(CHART, cls.plan, cls.now, replays=2000)
        cls.costly = tm.time_machine(CHART, cls.plan, cls.now, replays=2000,
                                     costs={"slippage_per_share": 0.05, "commission_per_share_usd": 0.005})

    def test_costs_never_change_the_odds(self):
        for k in ("p_target", "p_stop", "p_neither", "avg_R"):
            self.assertEqual(self.free["mc"][k], self.costly["mc"][k], k)

    def test_average_r_after_costs(self):
        m = self.costly["mc"]
        pt = m["p_target"]
        want = m["avg_R"] - pt * (0.05 + 0.01) / 5.86 - (1 - pt) * (0.10 + 0.01) / 5.86
        self.assertAlmostEqual(m["avg_R_net"], round(want, 3), places=3)
        self.assertLess(m["avg_R_net"], m["avg_R"])
        self.assertNotIn("avg_R_net", self.free["mc"])

    def test_history_after_costs(self):
        h = self.costly["history"]
        self.assertLess(h["avg_R_net"], h["avg_R"])
        self.assertTrue(all(x["R_net"] < x["R"] for x in h["trades"]))

    def test_the_report_says_which(self):
        self.assertIn("Costs: none a share set", "\n".join(tm.report(self.free)))
        text = "\n".join(tm.report(self.costly))
        self.assertIn("After costs:", text)
        self.assertIn("left out until trader-budget", text)

    def test_negative_costs_are_refused(self):
        with self.assertRaises(tm.TMError):
            tm.time_machine(CHART, self.plan, self.now, replays=10, costs={"slippage_per_share": -1})


if __name__ == "__main__":
    unittest.main()
