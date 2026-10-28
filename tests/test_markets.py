import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT

mk = load("trader-markets", "markets")

EXAMPLE = os.path.join(ROOT, "trader-markets", "instruments.example.json")
ACCT, INST, _ = mk.load(EXAMPLE)


def utc(text, tz="sgt"):
    return mk.parse_now(text, tz)


class Clocks(unittest.TestCase):
    def test_zones_either_side_of_the_daylight_changes(self):
        # 27 Oct 2026: London is back on GMT (25 Oct), New York still on EDT until 1 Nov
        t = utc("2026-10-27 17:00")
        self.assertEqual(mk.local(t, "ny").strftime("%H:%M"), "05:00")
        self.assertEqual(mk.local(t, "london").strftime("%H:%M"), "09:00")
        t = utc("2026-11-02 21:00")
        self.assertEqual(mk.local(t, "ny").strftime("%H:%M"), "08:00")
        t = utc("2026-07-01 21:00")
        self.assertEqual(mk.local(t, "london").strftime("%H:%M"), "14:00")

    def test_the_new_york_open_in_singapore(self):
        for day, sgt in (("2026-10-27 17:00", "Tue 27 Oct 21:30"), ("2026-11-02 17:00", "Mon 02 Nov 22:30")):
            is_on, nxt = mk.next_change("us_stock", utc(day))
            self.assertFalse(is_on)
            self.assertEqual(mk.fmt(nxt, "sgt"), sgt)

    def test_nyse_holidays_and_early_closes(self):
        self.assertFalse(mk.is_open("us_stock", mk.local(utc("2026-11-26 11:00", "ny"), "ny")))
        self.assertTrue(mk.is_open("us_stock", mk.local(utc("2026-11-27 12:59", "ny"), "ny")))
        self.assertFalse(mk.is_open("us_stock", mk.local(utc("2026-11-27 13:00", "ny"), "ny")))

    def test_fx_week(self):
        ny = lambda s: mk.local(utc(s, "ny"), "ny")
        self.assertFalse(mk.is_open("fx_24x5", ny("2026-10-31 12:00")))     # Saturday
        self.assertFalse(mk.is_open("fx_24x5", ny("2026-11-01 16:59")))     # Sunday before 17:00
        self.assertTrue(mk.is_open("fx_24x5", ny("2026-11-01 17:00")))
        self.assertTrue(mk.is_open("fx_24x5", ny("2026-11-04 17:30")))      # no daily break
        self.assertFalse(mk.is_open("fx_24x5", ny("2026-11-06 17:00")))     # Friday close

    def test_globex_daily_break(self):
        ny = lambda s: mk.local(utc(s, "ny"), "ny")
        self.assertTrue(mk.is_open("cme_globex", ny("2026-11-04 16:59")))
        self.assertFalse(mk.is_open("cme_globex", ny("2026-11-04 17:30")))
        self.assertTrue(mk.is_open("cme_globex", ny("2026-11-04 18:00")))
        self.assertFalse(mk.is_open("cme_globex", ny("2026-11-01 17:30")))  # Sunday opens 18:00
        is_on, nxt = mk.next_change("cme_globex", utc("2026-10-27 17:00"))
        self.assertTrue(is_on)
        self.assertEqual(mk.fmt(nxt, "sgt"), "Wed 28 Oct 05:00")

    def test_notes(self):
        self.assertEqual(mk.calendar_note("fx_24x5", utc("2026-10-27 17:00")),
                         "holiday hours are not modelled: check the broker")
        self.assertIsNone(mk.calendar_note("us_stock", utc("2026-10-27 17:00")))
        self.assertIn("2026 and 2027", mk.calendar_note("us_stock", utc("2028-03-01 17:00")))

    def test_bad_now(self):
        with self.assertRaises(mk.MarketError):
            mk.parse_now("27/10/2026")


class Money(unittest.TestCase):
    def test_example_file(self):
        self.assertEqual(ACCT, "USD")
        for s in ("TSLA", "SPCX", "NVDA", "MRVL", "US500", "NAS100", "US30", "XAUUSD", "EURUSD", "USDJPY"):
            self.assertIn(s, INST)
        self.assertEqual(len([s for s in INST.values() if s["asset_class"] == "us_stock"]), 14)

    def test_dollar_quoted(self):
        loss, rate = mk.loss_at_stop(INST["XAUUSD"], ACCT, 2400, 2395, 10)
        self.assertEqual((loss, rate), (50.0, 1.0))

    def test_yen_quoted_converts_at_the_stop(self):
        loss, rate = mk.loss_at_stop(INST["USDJPY"], ACCT, 152.40, 152.10, 10000)
        self.assertEqual(loss, 19.72)                        # 3,000 yen / 152.10
        self.assertAlmostEqual(rate, 1 / 152.10)

    def test_other_crosses_need_a_rate(self):
        spec = dict(INST["EURUSD"], symbol="EURGBP", quote_currency="GBP")
        with self.assertRaises(mk.MarketError):
            mk.loss_at_stop(spec, ACCT, 0.87, 0.868, 10000)
        self.assertEqual(mk.loss_at_stop(spec, ACCT, 0.87, 0.868, 10000, fx=1.30)[0], 26.0)

    def test_size_rounds_down_to_the_step(self):
        self.assertEqual(mk.size_for(INST["NVDA"], ACCT, 216, 214.5, 50)[0], 33)
        self.assertEqual(mk.size_for(INST["US500"], ACCT, 6800, 6790, 50)[0], 5.0)
        self.assertEqual(mk.size_for(INST["US500"], ACCT, 6800, 6787, 50)[0], 3.84)
        self.assertEqual(mk.size_for(INST["EURUSD"], ACCT, 1.1650, 1.1625, 50)[0], 20000)
        qty = mk.size_for(INST["USDJPY"], ACCT, 152.40, 152.10, 50)[0]
        self.assertEqual(qty, 25000)
        self.assertLessEqual(mk.loss_at_stop(INST["USDJPY"], ACCT, 152.40, 152.10, qty)[0], 50)

    def test_costs_count(self):
        spec = dict(INST["NVDA"], cost_per_trade=0.02)
        qty = mk.size_for(spec, ACCT, 216, 214.5, 50)[0]
        self.assertEqual(qty, 32)                           # 50 / 1.52
        self.assertEqual(mk.loss_at_stop(spec, ACCT, 216, 214.5, qty)[0], 48.64)

    def test_no_size_fits(self):
        self.assertEqual(mk.size_for(INST["MU"], ACCT, 903.7, 850, 50)[0], 0.0)

    def test_bad_specs_are_refused(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        for bad in ({"session": "lunch"}, {"session": "us_stock", "point_value": 0},
                    {"session": "us_stock", "cost_per_trade": -1}):
            p = os.path.join(tmp, "i.json")
            with open(p, "w") as fh:
                json.dump({"instruments": {"X": bad}}, fh)
            with self.assertRaises(mk.MarketError):
                mk.load(p)


class Cli(unittest.TestCase):
    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = mk.main(["--instruments", "/nonexistent.json"] + argv)
        return rc, out.getvalue(), err.getvalue()

    def test_clock(self):
        rc, out, _ = self.run_cli(["clock", "US500", "NVDA", "--now", "2026-10-27 17:00"])
        self.assertEqual(rc, 0)
        self.assertIn("NVDA    us_stock   closed opens Tue 27 Oct 21:30 SGT", out)
        self.assertIn("placeholders", out)

    def test_size_and_refusals(self):
        rc, out, _ = self.run_cli(["size", "--symbol", "xauusd", "--entry", "2400", "--stop", "2395",
                                   "--max-loss", "50"])
        self.assertIn("XAUUSD: 10", out)
        rc, _, err = self.run_cli(["risk", "--symbol", "ZZZZ", "--entry", "1", "--stop", "0.9", "--qty", "1"])
        self.assertEqual(rc, 2)
        self.assertIn("not in the instruments file", err)


class SameExample(unittest.TestCase):
    def test_journal_and_markets_share_one_example(self):
        with open(EXAMPLE) as a, open(os.path.join(ROOT, "trader-journal", "instruments.example.json")) as b:
            self.assertEqual(json.load(a), json.load(b))


if __name__ == "__main__":
    unittest.main()
