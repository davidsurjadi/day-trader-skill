"""The daily full-flow record: what blocks a ticket, how the day is judged, and the costs."""

import json
import os
import tempfile
import unittest

from _load import load

flow = load("trader-journal", "flow")


def bar(o, h, l, c):
    return {"o": o, "h": h, "l": l, "c": c}


class News(unittest.TestCase):
    def test_a_dividend_does_not_block_but_an_offering_does(self):
        news = {"items": [{"touches": ["ABBV"], "tags": ["capital"], "title": "AbbVie raises its dividend"},
                          {"touches": ["DXCM"], "tags": ["capital"], "title": "DexCom prices $500m offering"}]}
        self.assertEqual(flow.news_blocks(news, "ABBV"), ([], 1))
        self.assertEqual(flow.news_blocks(news, "DXCM"), (["offering"], 1))

    def test_a_halt_blocks(self):
        news = {"items": [{"touches": ["XYZ"], "tags": ["halt"], "title": "Trading halted"}]}
        self.assertEqual(flow.news_blocks(news, "XYZ")[0], ["halt"])


class Judge(unittest.TestCase):
    def test_stop_first_when_one_bar_touches_both(self):
        bars = {"10:15": bar(100, 106, 97, 101), "15:50": bar(101, 101, 101, 101)}
        self.assertEqual(flow.simulate(bars, 100, 98, 104, "10:15"), (98, "stop", "10:15"))

    def test_bars_before_the_decision_are_ignored(self):
        bars = {"10:10": bar(100, 100, 90, 100), "10:15": bar(100, 105, 99, 104), "15:50": bar(1, 1, 1, 1)}
        self.assertEqual(flow.simulate(bars, 100, 98, 104, "10:15"), (104, "target", "10:15"))

    def test_the_decision_is_the_first_close_after_the_range(self):
        self.assertEqual(flow.DECIDE_AT, "09:50")
        self.assertEqual(flow.clock_label("09:50"), "9:50 AM")
        self.assertEqual(flow.paper_now_utc("2026-10-29"), "2026-10-29T13:50:00Z")   # summer time
        self.assertEqual(flow.paper_now_utc("2026-11-02"), "2026-11-02T14:50:00Z")   # winter time

    def test_flat_at_the_1550_close(self):
        bars = {"10:15": bar(100, 101, 99, 100.5), "15:50": bar(100.5, 101, 100, 100.8)}
        self.assertEqual(flow.simulate(bars, 100, 98, 104, "10:15"), (100.8, "flat", "15:50"))

    def test_an_unfinished_session_is_refused(self):
        with self.assertRaises(flow.FlowError):
            flow.simulate({"10:15": bar(100, 101, 99, 100)}, 100, 98, 104, "10:15")


class Costs(unittest.TestCase):
    def test_saxo_minimum_and_percentage(self):
        self.assertEqual(flow.round_trip_cost(1, 28.0, 28.0), 2.18)               # USD 1 a side, plus 9%
        self.assertEqual(flow.round_trip_cost(16, 286.39, 292.47), 8.08)          # 0.08% of each side, plus 9%


class Close(unittest.TestCase):
    def test_logs_only_the_taken_tickets_with_their_tags(self):
        with tempfile.TemporaryDirectory() as d:
            day = "2026-10-28"
            os.makedirs(os.path.join(d, day))
            with open(os.path.join(d, day, "tickets.json"), "w") as fh:
                json.dump({"looked": [
                    {"rank": 1, "symbol": "AAA", "decision": "taken", "entry": 100.0, "stop": 98.0, "target": 104.0,
                     "qty": 20, "tm_hist_avg_r": 0.2, "news_items": 2},
                    {"rank": 2, "symbol": "BBB", "decision": "skipped", "reason": "budget"}]}, fh)
            saved = flow.paper.fetch_yahoo, flow.paper.session_bars
            flow.paper.fetch_yahoo = lambda sym: sym
            flow.paper.session_bars = lambda text, day: {"10:15": bar(100, 104.5, 99, 104), "15:50": bar(1, 1, 1, 1)}
            try:
                jpath = os.path.join(d, "j.json")
                self.assertEqual(flow.main(["close", "--date", day, "--dir", d, "--journal", jpath]), 0)
            finally:
                flow.paper.fetch_yahoo, flow.paper.session_bars = saved
            with open(jpath) as fh:
                trades = json.load(fh)["trades"]
            self.assertEqual([t["symbol"] for t in trades], ["AAA"])
            self.assertEqual((trades[0]["account"], trades[0]["exit"], trades[0]["tags"]["tm_hist_avg_r"]),
                             ("paper-flow", 104.0, 0.2))


if __name__ == "__main__":
    unittest.main()
