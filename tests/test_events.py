"""Event days: when results count, what an index gets, and that a saved session never changes."""

import json
import os
import tempfile
import unittest

from _load import load

ev = load("trader-journal", "events")


class Session(unittest.TestCase):
    def test_after_the_close_counts_for_the_next_session(self):
        self.assertEqual(ev.reaction_session("2026-07-29T20:04:53.000Z"), "2026-07-30")   # 16:04 New York

    def test_before_the_open_counts_the_same_day(self):
        self.assertEqual(ev.reaction_session("2026-10-28T10:45:00.000Z"), "2026-10-28")   # 06:45 New York

    def test_winter_time_is_five_hours(self):
        self.assertEqual(ev.reaction_session("2026-11-05T20:30:00.000Z"), "2026-11-05")   # 15:30 New York
        self.assertEqual(ev.reaction_session("2026-11-05T21:05:00.000Z"), "2026-11-06")   # 16:05 New York

    def test_a_friday_evening_counts_for_monday(self):
        self.assertEqual(ev.reaction_session("2026-10-30T20:10:00.000Z"), "2026-11-02")

    def test_a_holiday_is_skipped(self):
        self.assertEqual(ev.reaction_session("2026-11-25T21:10:00.000Z"), "2026-11-27")   # Thanksgiving


class Tags(unittest.TestCase):
    DOC = {"earnings": [{"symbol": "GEV"}], "unknown": [{"symbol": "TSM"}]}

    def test_earnings_wins_over_a_macro_day(self):
        self.assertEqual(ev.event_of("2026-10-28", "GEV", self.DOC), "earnings")
        self.assertEqual(ev.event_of("2026-10-28", "NVDA", self.DOC), "rate decision")

    def test_an_index_only_gets_macro_tags(self):
        self.assertEqual(ev.event_of("2026-11-10", "US500", None), "CPI")
        self.assertEqual(ev.event_of("2026-11-11", "NAS100", None), "none")

    def test_unknown_is_never_no_event(self):
        self.assertIsNone(ev.event_of("2026-10-28", "TSM", self.DOC))
        self.assertIsNone(ev.event_of("2026-10-29", "NVDA", None))       # the session was not saved


class FakeSec:
    def __init__(self):
        self.calls = 0

    def get(self, url):
        self.calls += 1
        if url == ev.TICKERS_URL:
            return {"0": {"ticker": "MSFT", "cik_str": 789019}, "1": {"ticker": "TSM", "cik_str": 1046179}}
        if "0000789019" in url:
            return {"filings": {"recent": {"form": ["8-K", "10-Q"], "items": ["2.02,9.01", ""],
                                           "acceptanceDateTime": ["2026-10-28T20:05:00.000Z", "2026-10-28T20:10:00.000Z"]}}}
        return {"filings": {"recent": {"form": ["6-K"], "items": [""], "acceptanceDateTime": ["2026-10-16T10:00:00.000Z"]}}}


class Cache(unittest.TestCase):
    def test_saves_once_and_never_rereads(self):
        with tempfile.TemporaryDirectory() as d:
            sec = FakeSec()
            doc = ev.cache("2026-10-29", d, ["MSFT", "TSM", "ZZZZ"], sec)
            self.assertEqual([e["symbol"] for e in doc["earnings"]], ["MSFT"])
            self.assertEqual(sorted(u["symbol"] for u in doc["unknown"]), ["TSM", "ZZZZ"])
            calls = sec.calls
            self.assertIsNone(ev.cache("2026-10-29", d, ["MSFT"], sec))
            self.assertEqual(sec.calls, calls)
            with open(os.path.join(d, "earnings-2026-10-29.json")) as fh:
                self.assertEqual(json.load(fh)["date"], "2026-10-29")

    def test_a_contact_is_required(self):
        with self.assertRaises(ev.EventError):
            ev.Sec("no-email-here")


if __name__ == "__main__":
    unittest.main()
