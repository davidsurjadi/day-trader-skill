"""The paper forward test's fixed rule, on synthetic 5-minute sessions."""

import datetime
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout

from _load import load, ROOT

paper = load("trader-journal", "paper")

DAY = "2026-10-21"                       # EDT: 9:30 New York = 13:30 UTC


def chart(path_points, day=DAY):
    """path_points: list of (high, low, close) per 5-minute bar from 9:30; open = previous close."""
    base = int((datetime.datetime.strptime(day, "%Y-%m-%d") + datetime.timedelta(hours=13, minutes=30)
                - datetime.datetime(1970, 1, 1)).total_seconds())
    ts, o, h, l, c = [], [], [], [], []
    prev = path_points[0][2]
    for i, (hi, lo, cl) in enumerate(path_points):
        ts.append(base + 300 * i)
        o.append(prev); h.append(hi); l.append(lo); c.append(cl)
        prev = cl
    return json.dumps({"chart": {"result": [{"meta": {"symbol": "^GSPC"}, "timestamp": ts, "indicators": {
        "quote": [{"open": o, "high": h, "low": l, "close": c, "volume": [0] * len(ts)}]}}]}})


def session(after):
    """A 78-bar session: a 100-110 opening range (stop 105), the bars in `after`, then quiet at 106."""
    bars = [(110, 100, 105), (108, 101, 104), (107, 102, 106)]
    bars += after
    bars += [(107, 106, 106)] * (78 - len(bars))
    return bars


class Rule(unittest.TestCase):
    def trade(self, after):
        return paper.orb_trade(paper.session_bars(chart(session(after)), DAY))

    def test_target(self):
        t, _ = self.trade([(111, 105, 112), (140, 111, 135)])
        self.assertEqual((t["entry"], t["stop"], t["target"]), (112, 105.0, 126))
        self.assertEqual((t["outcome"], t["exit"], t["entry_bar"]), ("target", 126, "09:45"))

    def test_stop(self):
        t, _ = self.trade([(113, 106, 112), (112, 104, 106)])
        self.assertEqual((t["outcome"], t["exit"]), ("stop", 105.0))

    def test_both_in_one_bar_is_the_stop(self):
        t, _ = self.trade([(113, 106, 112), (130, 100, 120)])
        self.assertEqual(t["outcome"], "stop")

    def test_flat_at_the_close(self):
        t, _ = self.trade([(113, 106, 112)])
        self.assertEqual((t["outcome"], t["exit_bar"], t["exit"]), ("flat", "15:50", 106))

    def test_a_poke_above_without_a_close_is_no_entry(self):
        t, why = self.trade([(115, 104, 109)])
        self.assertIsNone(t)
        self.assertIn("no 5-minute bar closed above", why)

    def test_incomplete_sessions_are_refused(self):
        bars = paper.session_bars(chart(session([(113, 106, 112)])[:40]), DAY)
        self.assertIsNone(paper.orb_trade(bars)[0])
        self.assertIn("not complete", paper.orb_trade(bars)[1])
        self.assertEqual(paper.session_bars(chart(session([])), "2026-10-22"), {})


class Cli(unittest.TestCase):
    def test_logs_once_with_cost_and_paper_account(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        f = os.path.join(tmp, "c.json")
        with open(f, "w") as fh:
            fh.write(chart(session([(111, 105, 112), (140, 111, 135)])))
        j = os.path.join(tmp, "journal.json")
        for expect in ("Logged", "Already in"):
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(paper.main(["run", "--date", DAY, "--chart", f, "--journal", j, "--cost", "0.7"]), 0)
            self.assertIn(expect, out.getvalue())
        with open(j) as fh:
            (t,) = json.load(fh)["trades"]
        self.assertEqual((t["account"], t["setup"], t["symbol"]), ("paper", "orb15-mid-2R", "US500"))
        self.assertEqual((t["r_gross"], t["r_net"]), (2.0, 1.9))          # 0.7 / 7 points of risk

    def test_no_trade_logs_nothing(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        f = os.path.join(tmp, "c.json")
        with open(f, "w") as fh:
            fh.write(chart(session([])))
        j = os.path.join(tmp, "journal.json")
        out = io.StringIO()
        with redirect_stdout(out):
            paper.main(["run", "--date", DAY, "--chart", f, "--journal", j])
        self.assertIn("no trade", out.getvalue())
        self.assertFalse(os.path.exists(j))


if __name__ == "__main__":
    unittest.main()
