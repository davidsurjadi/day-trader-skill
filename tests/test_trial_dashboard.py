"""The paper trial's dashboard data: small-account picks and the moomoo cost comparison."""

import unittest

from _load import load

td = load("trader-journal", "trial_dashboard")


def trade(sym, day, at, **kw):
    t = {"id": sym + day, "symbol": sym, "date": day, "tags": {"entry_time": at}, "qty": 10, "entry": 100.0, "exit": 102.0}
    t.update(kw)
    return t


class SmallAccount(unittest.TestCase):
    def test_first_signals_by_entry_bar_then_ticker(self):
        ts = [trade("BBB", "2026-10-29", "10:00"), trade("AAA", "2026-10-29", "10:00"),
              trade("CCC", "2026-10-29", "09:50"), trade("DDD", "2026-10-30", "11:00")]
        got = [(t["date"], t["symbol"]) for t in td.first_signals(ts, 2)]
        self.assertEqual(got, [("2026-10-29", "CCC"), ("2026-10-29", "AAA"), ("2026-10-30", "DDD")])


class Moomoo(unittest.TestCase):
    def test_round_trip_fees(self):
        # 10 shares, 1,000 in and 1,020 out: platform 2 x 0.99 x 1.09 = 2.1582, settlement 0.03 + 0.03,
        # SEC max(0.01, 0.0000206 x 1020) = 0.021, TAF max(0.01, 0.00195) = 0.01, CAT 0.00006.
        self.assertEqual(td.moomoo_costs(trade("AAA", "2026-10-29", "10:00")), 2.25)


if __name__ == "__main__":
    unittest.main()
