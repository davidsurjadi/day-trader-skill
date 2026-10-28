#!/usr/bin/env python3
"""Event days for the paper trial: earnings, rate decisions and CPI. Standard library only.

A tag, never a filter: it changes no trade. After the trial the record can be read
with and without event days, from the same trades (David chose this on 28 Oct 2026).

  rate decision  the second day of an FOMC meeting (statement 14:00 New York), from
                 federalreserve.gov/monetarypolicy/fomccalendars.htm, read 28 Oct 2026
  CPI            the BLS CPI release day (08:30 New York, before the open), from
                 bls.gov/schedule/news_release/cpi.htm, read 28 Oct 2026
  earnings       a stock's first session after its results, from the time SEC accepted
                 its 8-K with item 2.02 (results of operations): before 16:00 New York
                 that session, at or after 16:00 the next one. Saved once per session
                 in the events folder, so a tag never changes afterwards.

Foreign companies that report on 6-K (TSM, ASML and others) and tickers SEC does not
list get no earnings tag: they count as unknown, never as "no event". A stock's
earnings tag wins over a macro tag; an index only gets macro tags.

SEC asks every automated request to carry a contact (its fair-access rules):
--user-agent "Name email@example.com". It is sent to sec.gov only.

  python3 events.py cache --date 2026-10-29 --dir paper/events --user-agent "Name email" \\
      --watchlist trader-scanner/watchlist.example.txt trader-scanner/watchlist-broad.txt
  python3 events.py show --date 2026-10-29 --dir paper/events --symbols META MSFT US500
"""

import argparse
import datetime
import json
import os
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
MACRO = {
    "2026-10-28": "rate decision",
    "2026-11-10": "CPI",
    "2026-12-09": "rate decision",
    "2026-12-10": "CPI",
    "2027-01-13": "CPI",
    "2027-01-27": "rate decision",
}
MACRO_YEARS = (2026, 2027)     # the trial runs 28 Oct 2026 to 28 Jan 2027
INDEX_SYMBOLS = ("US500", "NAS100", "US30")
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK%010d.json"

try:                                              # the NYSE calendar kept in trader-markets
    sys.path.insert(0, os.path.join(os.path.dirname(HERE), "trader-markets"))
    from markets import NYSE_HOLIDAYS  # noqa: E402
except ImportError:                               # this folder copied on its own: weekdays only
    NYSE_HOLIDAYS = {}


class EventError(Exception):
    pass


# ---------------------------------------------------------------- time (New York, no tz files)

def _nth_sunday(year, month, n):
    d = datetime.date(year, month, 1)
    d += datetime.timedelta(days=(6 - d.weekday()) % 7)
    return d + datetime.timedelta(weeks=n - 1)


def to_eastern(utc):
    y = utc.year
    start = datetime.datetime.combine(_nth_sunday(y, 3, 2), datetime.time(7, 0))
    end = datetime.datetime.combine(_nth_sunday(y, 11, 1), datetime.time(6, 0))
    return utc + datetime.timedelta(hours=-4 if start <= utc < end else -5)


def is_session(d):
    return d.weekday() < 5 and d.isoformat() not in NYSE_HOLIDAYS


def next_session(d):
    d += datetime.timedelta(days=1)
    while not is_session(d):
        d += datetime.timedelta(days=1)
    return d


def reaction_session(accepted_utc):
    """SEC acceptance time (UTC, ISO) -> the first New York session that can trade on it."""
    t = datetime.datetime.strptime(accepted_utc[:19], "%Y-%m-%dT%H:%M:%S")
    ny = to_eastern(t)
    d = ny.date()
    if not is_session(d) or ny.time() >= datetime.time(16, 0):
        d = next_session(d)
    return d.isoformat()


# ---------------------------------------------------------------- SEC

class Sec:
    def __init__(self, user_agent, pause=0.15):
        if not user_agent or "@" not in user_agent:
            raise EventError('SEC needs a contact: --user-agent "Your Name you@example.com"')
        self.ua, self.pause = user_agent, pause

    def get(self, url):
        time.sleep(self.pause)                    # SEC allows 10 requests a second
        req = urllib.request.Request(url, headers={"User-Agent": self.ua, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise EventError("could not read %s: %s" % (url, e))


def earnings_filings(sub):
    """A company's submissions JSON -> (files 8-K at all, [acceptance times of 8-Ks with item 2.02])."""
    r = (sub.get("filings") or {}).get("recent") or {}
    forms, items, acc = r.get("form", []), r.get("items", []), r.get("acceptanceDateTime", [])
    files_8k = "8-K" in forms
    out = [acc[i] for i, f in enumerate(forms) if f == "8-K" and "2.02" in str(items[i] if i < len(items) else "")]
    return files_8k, out


def read_watchlists(paths):
    syms = []
    for p in paths:
        with open(p, encoding="utf-8") as fh:
            for ln in fh:
                s = ln.split("|")[0].strip().upper()
                if s and not s.startswith("#") and s not in syms:
                    syms.append(s)
    return syms


def _path(cache_dir, day):
    return os.path.join(cache_dir, "earnings-%s.json" % day)


def cache(day, cache_dir, symbols, sec):
    """Work out which of `symbols` have `day` as their first session after results, and save
    it. A saved session is never worked out again, so its tags stay as first read."""
    p = _path(cache_dir, day)
    if os.path.exists(p):
        return None
    ciks = {v["ticker"].upper(): int(v["cik_str"]) for v in sec.get(TICKERS_URL).values()}
    earnings, unknown = [], []
    for sym in symbols:
        cik = ciks.get(sym) or ciks.get(sym.replace(".", "-"))
        if not cik:
            unknown.append({"symbol": sym, "why": "not in SEC's ticker list"})
            continue
        files_8k, accepted = earnings_filings(sec.get(SUBMISSIONS_URL % cik))
        if not files_8k:
            unknown.append({"symbol": sym, "why": "files no 8-K (a foreign filer)"})
            continue
        hits = [a for a in accepted if reaction_session(a) == day]
        if hits:
            earnings.append({"symbol": sym, "accepted_utc": hits[0]})
    os.makedirs(cache_dir, exist_ok=True)
    doc = {"date": day, "source": "SEC EDGAR submissions, 8-K item 2.02",
           "read_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
           "earnings": earnings, "unknown": unknown}
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1)
        fh.write("\n")
    return doc


def load_day(cache_dir, day):
    p = _path(cache_dir, day)
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


def event_of(day, symbol, doc):
    """'earnings', 'rate decision', 'CPI', 'none', or None when it cannot be known.
    doc is the session's saved earnings file (None if not saved)."""
    sym = str(symbol).upper()
    if sym not in INDEX_SYMBOLS:
        if doc is None or sym in {u["symbol"] for u in doc.get("unknown", [])}:
            return None
        if sym in {e["symbol"] for e in doc.get("earnings", [])}:
            return "earnings"
    if datetime.date.fromisoformat(day).year not in MACRO_YEARS:
        return None
    return MACRO.get(day, "none")


def main(argv=None, sec=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("cache")
    c.add_argument("--date", required=True, help="the New York session, YYYY-MM-DD")
    c.add_argument("--dir", default="paper/events")
    c.add_argument("--watchlist", nargs="+", required=True)
    c.add_argument("--user-agent", help='SEC contact: "Your Name you@example.com"')
    s = sub.add_parser("show")
    s.add_argument("--date", required=True)
    s.add_argument("--dir", default="paper/events")
    s.add_argument("--symbols", nargs="+", required=True)
    a = ap.parse_args(argv)
    try:
        datetime.date.fromisoformat(a.date)
        if a.cmd == "cache":
            doc = cache(a.date, a.dir, read_watchlists(a.watchlist), sec or Sec(a.user_agent))
            if doc is None:
                print("%s: already saved, left as it was" % a.date)
            else:
                print("%s: %d stocks with earnings (%s); %d unknown" % (
                    a.date, len(doc["earnings"]), ", ".join(e["symbol"] for e in doc["earnings"]) or "none",
                    len(doc["unknown"])))
        else:
            doc = load_day(a.dir, a.date)
            for sym in a.symbols:
                print("%s %s: %s" % (sym.upper(), a.date, event_of(a.date, sym, doc) or "unknown"))
        return 0
    except (EventError, ValueError, OSError) as e:
        print("events: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
