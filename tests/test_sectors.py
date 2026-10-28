"""The sector cap in trader-risk: at most max_open_per_sector stocks of one sector open at a time."""

import json
import os
import unittest

from _load import load, ROOT

check = load("trader-risk", "check")

CFG = os.path.join(ROOT, "trader-budget", "config.example.json")
NOW = check.parse_now("2026-10-19 10:15")
TODAY = "2026-10-19"


def cfg(**over):
    with open(CFG) as fh:
        c = json.load(fh)
    c.update({"min_reward_risk": 0, "max_trades_per_day": 0, "max_daily_loss_usd": 0, "max_position_pct": 100,
              "max_order_usd": 100000, "budget": 100000})
    c.update(over)
    return c


def bracket(sym, qty=1, entry=100.0, stop=99.0, target=102.0):
    return {"symbol": sym, "side": "buy", "qty": str(qty), "type": "market", "time_in_force": "day",
            "order_class": "bracket", "take_profit_limit_price": "%.2f" % target,
            "stop_loss_stop_price": "%.2f" % stop, "plan": {"entry": entry, "stop": stop, "target": target}}


def holding(*syms):
    return {"cash": 100000, "positions": [{"ticker": s, "shares": 1, "lots": [{"shares": 1, "cost": 100}]}
                                          for s in syms]}


def run(orders, portfolio=None, state=None, **over):
    prices = {s: 100.0 for s in ("NVDA", "AMD", "TSLA", "MU", "KO")}
    res = check.check(orders, cfg(**over), portfolio=portfolio, prices=prices, state=state, today=TODAY, now=NOW)
    return [r["verdict"] for r in res["results"]], " | ".join(" ".join(r["reasons"]) for r in res["results"])


class SectorCap(unittest.TestCase):
    def test_the_example_config_carries_the_ai_list(self):
        c = cfg()
        self.assertEqual(c["max_open_per_sector"], 2)
        self.assertEqual(len(c["sectors"]["ai_tech"]), 14)

    def test_a_third_ai_stock_is_blocked(self):
        v, why = run([bracket("TSLA")], portfolio=holding("NVDA", "AMD"))
        self.assertEqual(v, ["BLOCK"])
        self.assertIn("sector: 2 ai_tech stocks already open (AMD, NVDA)", why)

    def test_adding_to_one_already_open_is_not_a_new_bet(self):
        self.assertEqual(run([bracket("NVDA")], portfolio=holding("NVDA", "AMD"))[0], ["PASS"])

    def test_other_sectors_and_unlisted_stocks_pass(self):
        self.assertEqual(run([bracket("KO")], portfolio=holding("NVDA", "AMD"))[0], ["PASS"])

    def test_the_batch_counts(self):
        v, why = run([bracket("NVDA"), bracket("AMD"), bracket("MU")])
        self.assertEqual(v, ["PASS", "PASS", "BLOCK"])
        self.assertIn("(AMD, NVDA)", why)

    def test_placed_today_counts_until_sold(self):
        placed = {"orders": [{"date": TODAY, "status": "placed", "kind": "entry", "symbol": s, "side": "buy",
                              "max_loss": 1} for s in ("NVDA", "AMD")]}
        self.assertEqual(run([bracket("MU")], state=placed)[0], ["BLOCK"])
        placed["orders"].append({"date": TODAY, "status": "filled", "symbol": "AMD", "side": "sell",
                                 "qty": "1", "price": 101})
        self.assertEqual(run([bracket("MU")], state=placed)[0], ["PASS"])

    def test_off_when_zero_or_no_sectors(self):
        self.assertEqual(run([bracket("TSLA")], portfolio=holding("NVDA", "AMD"), max_open_per_sector=0)[0],
                         ["PASS"])
        self.assertEqual(run([bracket("TSLA")], portfolio=holding("NVDA", "AMD"), sectors={})[0], ["PASS"])

    def test_getting_out_is_never_blocked(self):
        sell = {"symbol": "NVDA", "side": "sell", "qty": "1", "type": "market", "time_in_force": "day"}
        self.assertEqual(run([sell], portfolio=holding("NVDA", "AMD", "TSLA"))[0], ["PASS"])

    def test_a_ticker_in_two_sectors_is_a_config_error(self):
        with self.assertRaises(check.RiskError):
            run([bracket("KO")], sectors={"a": ["NVDA"], "b": ["nvda"]})


if __name__ == "__main__":
    unittest.main()
