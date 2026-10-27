"""Each skill carries its own copy of the NYSE calendar so a single folder still
works when copied on its own. These tests stop the copies drifting apart."""

import unittest

from _load import load

COPIES = [("trader-alerts", "alerts"), ("trader-entry", "entry"), ("trader-exit", "exit"),
          ("trader-risk", "check"), ("trader-scanner", "scan"),
          ("trader-markets", "markets")]


class CalendarCopies(unittest.TestCase):
    def test_holiday_tables_match(self):
        mods = [(f, load(f, n)) for f, n in COPIES]
        first_folder, first = mods[0]
        for folder, mod in mods[1:]:
            self.assertEqual(sorted(mod.NYSE_HOLIDAYS), sorted(first.NYSE_HOLIDAYS),
                             "%s's holiday dates differ from %s's" % (folder, first_folder))

    def test_calendar_years_match(self):
        years = {f: getattr(load(f, n), "CALENDAR_YEARS", None) for f, n in COPIES}
        known = {y for y in years.values() if y is not None}
        self.assertEqual(len(known), 1, years)


if __name__ == "__main__":
    unittest.main()
