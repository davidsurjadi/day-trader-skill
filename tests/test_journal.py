import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT

journal = load("trader-journal", "journal")

INSTRUMENTS = os.path.join(ROOT, "trader-journal", "instruments.example.json")


def trade(**over):
    kw = dict(symbol="TSLA", setup="orb", side="long", entry=364.18, stop=358.32, exit_price=370.04,
              qty=8, date="2026-10-19", costs=0)
    kw.update(over)
    return journal.make_trade(**kw)


class OneTrade(unittest.TestCase):
    def test_the_reel_trade_at_its_target_is_plus_one_r(self):
        t = trade()
        self.assertEqual(t["risk"], 46.88)
        self.assertEqual(t["gross"], 46.88)
        self.assertEqual(t["r_gross"], 1.0)
        self.assertEqual(t["r_net"], 1.0)

    def test_costs_come_off_net_and_r(self):
        t = trade(costs=4.688)
        self.assertEqual(t["net"], 42.19)
        self.assertEqual(t["r_net"], 0.9)
        self.assertEqual(t["r_gross"], 1.0)

    def test_a_stopped_out_trade_is_minus_one_r(self):
        self.assertEqual(trade(exit_price=358.32)["r_gross"], -1.0)

    def test_short_trades_count_the_other_way(self):
        t = trade(side="short", entry=100, stop=102, exit_price=96, qty=10)
        self.assertEqual((t["risk"], t["gross"], t["r_gross"]), (20.0, 40.0, 2.0))

    def test_point_value_from_the_instruments_file(self):
        inst = journal.load_instruments(INSTRUMENTS)
        t = trade(symbol="XAUUSD", entry=2400.0, stop=2395.0, exit_price=2410.0, qty=0.1, instruments=inst)
        self.assertEqual(t["point_value"], 100)
        self.assertEqual(t["asset_class"], "gold")
        self.assertEqual((t["risk"], t["gross"], t["r_gross"]), (50.0, 100.0, 2.0))

    def test_a_trade_without_a_real_stop_has_no_r(self):
        for bad in (dict(stop=364.18), dict(stop=370.0), dict(side="short", stop=358.0)):
            with self.assertRaises(journal.JournalError):
                trade(**bad)

    def test_refuses_bad_numbers(self):
        for bad in (dict(qty=0), dict(exit_price="x"), dict(costs=-1), dict(point_value=0), dict(side="up"),
                    dict(date="19/10/2026"), dict(symbol=" ")):
            with self.assertRaises(journal.JournalError):
                trade(**bad)

    def test_same_trade_same_id(self):
        self.assertEqual(trade()["id"], trade()["id"])
        self.assertNotEqual(trade()["id"], trade(exit_price=360)["id"])


class Stats(unittest.TestCase):
    def test_wilson_interval(self):
        self.assertEqual(journal.wilson(0, 0), (None, None))
        lo, hi = journal.wilson(13, 28)
        self.assertAlmostEqual(lo, 0.2953, places=3)
        self.assertAlmostEqual(hi, 0.6419, places=3)
        self.assertEqual(journal.wilson(5, 5)[1], 1.0)

    def test_drawdown_and_losing_run(self):
        self.assertEqual(journal.drawdown([1, -1, -1, -1, 2, -1]), (-3.0, 3))

    def test_a_coin_flip_record_shows_no_edge(self):
        trades = [trade(exit_price=370.04, date="2026-10-%02d" % (i + 1)) for i in range(13)]
        trades += [trade(exit_price=358.32, date="2026-11-%02d" % (i + 1)) for i in range(15)]
        s = journal.summarize(trades)
        self.assertEqual((s["trades"], s["wins"]), (28, 13))
        self.assertTrue(s["verdict"].startswith("no edge shown yet"))
        lo, hi = s["avg_r_net_95"]
        self.assertLess(lo, 0)
        self.assertGreater(hi, 0)

    def test_costs_can_turn_an_edge_into_a_loss(self):
        rs = [1.2, -1.0] * 15 + [1.2] * 10
        days = ["2026-%02d-%02d" % (1 + i // 28, 1 + i % 28) for i in range(len(rs))]
        gross = [trade(exit_price=round(364.18 + r * 5.86, 2), date=d) for r, d in zip(rs, days)]
        self.assertTrue(journal.summarize(gross)["verdict"].startswith("the record shows an edge"))
        costly = [trade(exit_price=round(364.18 + r * 5.86, 2), date=d, costs=46.88) for r, d in zip(rs, days)]
        s = journal.summarize(costly)
        self.assertLess(s["avg_r_net"], s["avg_r_gross"])
        self.assertFalse(s["verdict"].startswith("the record shows an edge"))

    def test_one_trade_is_not_a_record(self):
        self.assertTrue(journal.summarize([trade()])["verdict"].startswith("one trade"))
        self.assertEqual(journal.summarize([])["verdict"], "no trades yet")

    def test_grouping_and_filters(self):
        j = {"trades": [trade(), trade(setup="flag", date="2026-10-20"),
                        trade(symbol="NVDA", entry=100, stop=99, exit_price=101, qty=1, account="demo")]}
        res = journal.stats(j, by="setup")
        self.assertEqual(sorted(res["groups"]), ["flag", "orb"])
        self.assertEqual(res["groups"]["orb"]["trades"], 2)
        self.assertEqual(journal.stats(j, account="demo")["all"]["trades"], 1)
        self.assertEqual(journal.stats(j, since="2026-10-20")["all"]["trades"], 1)
        self.assertIn("verdict", journal.report(res))


class ImportFills(unittest.TestCase):
    STATE = {"orders": [
        {"date": "2026-10-19", "status": "approved", "kind": "entry", "symbol": "TSLA", "stop": 358.32},
        {"date": "2026-10-19", "status": "filled", "symbol": "TSLA", "side": "buy", "qty": "8", "price": 364.20},
        {"date": "2026-10-19", "status": "filled", "symbol": "TSLA", "side": "sell", "qty": "5", "price": 370.04},
        {"date": "2026-10-19", "status": "filled", "symbol": "TSLA", "side": "sell", "qty": "3", "price": 358.32},
        {"date": "2026-10-19", "status": "filled", "symbol": "AAPL", "side": "buy", "qty": "2", "price": 250.0},
        {"date": "2026-10-19", "status": "filled", "symbol": "AAPL", "side": "sell", "qty": "2", "price": 251.0},
        {"date": "2026-10-19", "status": "filled", "symbol": "KO", "side": "buy", "qty": "4", "price": 70.0},
    ]}

    def test_pairs_fifo_and_never_guesses_a_stop(self):
        trades, skipped, still_open = journal.pair_fills(self.STATE, plan={"symbol": "TSLA", "setup": "orb"})
        self.assertEqual([(t["symbol"], t["qty"], t["exit"]) for t in trades],
                         [("TSLA", 5.0, 370.04), ("TSLA", 3.0, 358.32)])
        self.assertEqual({t["setup"] for t in trades}, {"orb"})
        self.assertEqual(len(skipped), 1)
        self.assertIn("AAPL", skipped[0])
        self.assertEqual(still_open, ["KO 4"])

    def test_cli_import_is_idempotent_and_stats_print(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        state = os.path.join(tmp, "broker.json")
        with open(state, "w") as fh:
            json.dump(self.STATE, fh)
        jpath = os.path.join(tmp, "journal.json")
        args = ["import", "--state", state, "--journal", jpath, "--setup", "orb",
                "--plan", os.path.join(tmp, "none.json"), "--instruments", os.path.join(tmp, "none.json")]
        for expect in ("Imported 2 new trades", "Imported 0 new trades"):
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(journal.main(args), 0)
            self.assertIn(expect, out.getvalue())
        with open(jpath) as fh:
            self.assertEqual(len(json.load(fh)["trades"]), 2)
        out = io.StringIO()
        with redirect_stdout(out):
            journal.main(["stats", "--journal", jpath, "--json"])
        self.assertEqual(json.loads(out.getvalue())["all"]["trades"], 2)

    def test_cli_add_rejects_with_a_reason(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        err = io.StringIO()
        with redirect_stderr(err):
            rc = journal.main(["add", "--journal", os.path.join(tmp, "j.json"), "--symbol", "TSLA",
                               "--entry", "364", "--stop", "370", "--exit", "380", "--qty", "1",
                               "--date", "2026-10-19"])
        self.assertEqual(rc, 2)
        self.assertIn("wrong side", err.getvalue())


if __name__ == "__main__":
    unittest.main()
