#!/usr/bin/env python3
"""Paper forward test for trader-journal: one fixed rule, one instrument, one session
at a time, logged to the journal as a paper trade. Standard library only. It never
places an order and never reads an account.

The rule is fixed in this file on purpose, so the record cannot drift toward whatever
looked good last week:

  opening-range breakout, long only, 5-minute bars, New York time
  range     the first 15 minutes (9:30 to 9:45): highest high, lowest low
  entry     the close of the first bar after the range that CLOSES above the range high
  stop      the range midpoint, rounded down to the cent
  target    2R (2 x entry minus stop) above the entry
  exit      the stop or the target, whichever a bar touches first; a bar that touches
            both counts as the stop; else the close of the 15:50 bar (flat by 15:55)

    python3 paper.py run --date 2026-10-28                     # Yahoo ^GSPC, logs US500 to trader/journal.json
    python3 paper.py run --date 2026-10-28 --chart saved.json  # a saved Yahoo-style chart (e.g. saxo.py bars --out)
    python3 paper.py run --date 2026-10-28 --dry-run           # show, do not log
    python3 paper.py stocks --date 2026-10-28 --watchlist ../trader-scanner/watchlist.example.txt \
        --journal trader/stocks-journal.json                   # the same rule on each stock, sized and charged

One trade is logged per session at most, as qty 1 and point value 1, so its R is
exact whatever the contract size; --cost is the round trip in price points (the
US500.I spread). A session with no breakout logs nothing and says so.

Stocks (the stocks command) use the same rule, sized and charged like a real
trade: whole shares, as many as fit both the max loss at the stop (--max-loss,
50) and the position cap (--position-cap, 3750); commission on each side of
--commission-pct (0.08) of the value, at least --commission-min (USD 1), plus
--tax-pct (9) on the commission. Those defaults are Saxo SG Classic for US
shares and Singapore GST, as of 28 Oct 2026. A stock whose stop is too far
for even one share inside the limits logs nothing and says so.
"""

import argparse
import datetime
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import journal  # noqa: E402  (the journal next to this file)

RULE = {"name": "orb15-mid-2R", "or_minutes": 15, "target_r": 2.0, "flat_bar": "15:50"}
YAHOO = "https://query2.finance.yahoo.com/v8/finance/chart/%s?range=5d&interval=5m"
UA = "Mozilla/5.0 (compatible; day-trader-skill/2.0; +https://github.com/Jakeschincariol/day-trader-skill)"


class PaperError(Exception):
    pass


# ---------------------------------------------------------------- time (New York, no tz files)

def _nth_sunday(year, month, n):
    d = datetime.date(year, month, 1)
    d += datetime.timedelta(days=(6 - d.weekday()) % 7)
    return d + datetime.timedelta(weeks=n - 1)


def eastern_offset(utc):
    y = utc.year
    start = datetime.datetime.combine(_nth_sunday(y, 3, 2), datetime.time(7, 0))
    end = datetime.datetime.combine(_nth_sunday(y, 11, 1), datetime.time(6, 0))
    return -4 if start <= utc < end else -5


def to_eastern(utc):
    return utc + datetime.timedelta(hours=eastern_offset(utc))


# ---------------------------------------------------------------- bars

def fetch_yahoo(symbol):
    req = urllib.request.Request(YAHOO % urllib.request.quote(symbol), headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.read().decode("utf-8")
    except (urllib.error.URLError, OSError) as e:
        raise PaperError("could not fetch %s from Yahoo: %s" % (symbol, e))


def session_bars(text, day):
    """Yahoo-style chart JSON -> the day's regular-session 5-minute bars, keyed "HH:MM" (New York)."""
    try:
        res = json.loads(text)["chart"]["result"][0]
    except (ValueError, KeyError, IndexError, TypeError):
        raise PaperError("the chart is not a Yahoo-style chart JSON")
    q = res["indicators"]["quote"][0]
    out = {}
    for i, ts in enumerate(res.get("timestamp") or []):
        o, h, l, c = (q[k][i] for k in ("open", "high", "low", "close"))
        if None in (o, h, l, c) or ts % 300:
            continue
        ny = to_eastern(datetime.datetime(1970, 1, 1) + datetime.timedelta(seconds=ts))
        if ny.date().isoformat() != day:
            continue
        hm = ny.strftime("%H:%M")
        if "09:30" <= hm <= "15:55":
            out[hm] = {"o": o, "h": h, "l": l, "c": c}
    return out


def _slot(n):
    m = 9 * 60 + 30 + 5 * n
    return "%02d:%02d" % (m // 60, m % 60)


# ---------------------------------------------------------------- the rule

def orb_trade(bars):
    """The fixed rule on one session's bars -> a trade dict, or (None, reason)."""
    n = RULE["or_minutes"] // 5
    first = [_slot(i) for i in range(n)]
    if any(s not in bars for s in first):
        return None, "the opening range is incomplete (missing %s)" % ", ".join(s for s in first if s not in bars)
    last = RULE["flat_bar"]
    if last not in bars:
        return None, "the session is not complete (no %s bar yet)" % last
    oh = max(bars[s]["h"] for s in first)
    ol = min(bars[s]["l"] for s in first)
    slots = [_slot(i) for i in range(n, 76)]          # 9:45 up to and including 15:50
    k = next((i for i, s in enumerate(slots) if s in bars and bars[s]["c"] > oh), None)
    if k is None:
        return None, "no 5-minute bar closed above the opening-range high %.2f" % oh
    entry = round(bars[slots[k]]["c"], 2)
    stop = math.floor((oh + ol) / 2.0 * 100) / 100.0
    if stop >= entry:
        return None, "the stop %.2f is not below the entry %.2f" % (stop, entry)
    target = round(entry + RULE["target_r"] * (entry - stop), 2)
    exit_price, how, at = None, "flat", last
    for s in slots[k + 1:]:
        b = bars.get(s)
        if not b:
            continue
        if b["l"] <= stop:
            exit_price, how, at = stop, "stop", s
            break
        if b["h"] >= target:
            exit_price, how, at = target, "target", s
            break
    if exit_price is None:
        exit_price = round(bars[last]["c"], 2)
    return {"entry": entry, "stop": stop, "target": target, "exit": exit_price, "outcome": how,
            "entry_bar": slots[k], "exit_bar": at, "or_high": round(oh, 2), "or_low": round(ol, 2)}, None


# ---------------------------------------------------------------- command

def stock_size(t, a):
    """(whole shares, commission + tax for the round trip in USD), or (0, reason)."""
    risk = t["entry"] - t["stop"]
    qty = min(math.floor(a.max_loss / risk + 1e-9), math.floor(a.position_cap / t["entry"] + 1e-9))
    if qty < 1:
        return 0, ("no whole share fits: %.2f risk a share against the %.2f max loss, %.2f a share against the "
                   "%.2f position cap" % (risk, a.max_loss, t["entry"], a.position_cap))
    side = lambda price: max(a.commission_min, a.commission_pct / 100.0 * qty * price)
    return qty, round((side(t["entry"]) + side(t["exit"])) * (1 + a.tax_pct / 100.0), 2)


def log_session(a, symbol, text, source, day, stock):
    """The rule on one symbol's session -> one printed line; logs it unless --dry-run. Returns 0."""
    bars = session_bars(text, day)
    if not bars:
        print("%s %s: no regular-session bars in the data (a holiday, a weekend, or not traded yet). Nothing logged."
              % (symbol, day))
        return 0
    t, why = orb_trade(bars)
    if t is None:
        print("%s %s: no trade, %s. Nothing logged." % (symbol, day, why))
        return 0
    if stock:
        qty, costs = stock_size(t, a)
        if not qty:
            print("%s %s: no trade, %s. Nothing logged." % (symbol, day, costs))
            return 0
        cost_words = "USD %.2f commission and tax on %d shares" % (costs, qty)
    else:
        qty, costs = 1, a.cost
        cost_words = "the %.2f-point cost" % a.cost
    rec = journal.make_trade(symbol, RULE["name"], "long", t["entry"], t["stop"], t["exit"], qty, day,
                             costs=costs, target=t["target"], point_value=1,
                             asset_class="us_stock" if stock else "index_cfd", account="paper",
                             note="%s; entry %s, %s at %s; range %.2f-%.2f"
                             % (source, t["entry_bar"], t["outcome"], t["exit_bar"], t["or_low"], t["or_high"]),
                             source="paper.py")
    line = ("%s %s: entry %.2f at the %s bar close, stop %.2f, target %.2f -> %s at %s, exit %.2f: %+.2fR gross, "
            "%+.2fR after %s"
            % (symbol, day, t["entry"], t["entry_bar"], t["stop"], t["target"], t["outcome"], t["exit_bar"],
               t["exit"], rec["r_gross"], rec["r_net"], cost_words))
    if a.dry_run:
        print(line + " (dry run, not logged)")
        return 0
    doc = journal.load_journal(a.journal)
    added = journal.add_trades(doc, [rec])
    if added:
        journal.write_json(a.journal, doc)
    print(line + (". Logged to %s." % a.journal if added else ". Already in %s." % a.journal))
    return 0


def run(a):
    day = journal._date(a.date, "--date")
    if a.chart:
        with open(a.chart, encoding="utf-8") as fh:
            text, source = fh.read(), "file:" + os.path.basename(a.chart)
    else:
        text, source = fetch_yahoo(a.source), "yahoo:" + a.source
    return log_session(a, a.symbol.upper(), text, source, day, stock=False)


def read_watchlist(path):
    with open(path, encoding="utf-8") as fh:
        return [ln.split("|")[0].strip().upper() for ln in fh
                if ln.strip() and not ln.strip().startswith("#")]


def stocks(a):
    day = journal._date(a.date, "--date")
    syms = read_watchlist(a.watchlist)
    if not syms:
        raise PaperError("no tickers in %s" % a.watchlist)
    failed = []
    for i, sym in enumerate(syms):
        if a.chart_dir:
            path = os.path.join(a.chart_dir, "%s.json" % sym)
            if not os.path.exists(path):
                print("%s %s: no chart file %s. Nothing logged." % (sym, day, path))
                failed.append(sym)
                continue
            with open(path, encoding="utf-8") as fh:
                text, source = fh.read(), "file:" + os.path.basename(path)
        else:
            if i:
                time.sleep(a.pause)
            try:
                text, source = fetch_yahoo(sym), "yahoo:" + sym
            except PaperError as e:
                print("%s %s: %s. Nothing logged." % (sym, day, e))
                failed.append(sym)
                continue
        log_session(a, sym, text, source, day, stock=True)
    if failed:
        print("No data for %d of %d: %s." % (len(failed), len(syms), ", ".join(failed)))
        return 1
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Paper forward test: the fixed US500 opening-range rule, one session.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run", help="apply the rule to one New York session and log it")
    p.add_argument("--date", required=True, help="the New York session date, YYYY-MM-DD")
    p.add_argument("--symbol", default="US500", help="the name logged in the journal")
    p.add_argument("--source", default="^GSPC", help="the Yahoo symbol to read when no --chart is given")
    p.add_argument("--chart", help="a saved Yahoo-style chart JSON instead of fetching")
    p.add_argument("--cost", type=float, default=0.6, help="round-trip cost in price points (default 0.6)")
    p.add_argument("--journal", default="trader/journal.json")
    p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("stocks", help="the same rule on every stock in a watchlist, sized and charged")
    p.add_argument("--date", required=True, help="the New York session date, YYYY-MM-DD")
    p.add_argument("--watchlist", required=True, help="a text file of tickers, one a line, # for comments")
    p.add_argument("--chart-dir", help="read <TICKER>.json from this folder instead of fetching")
    p.add_argument("--max-loss", type=float, default=50.0)
    p.add_argument("--position-cap", type=float, default=3750.0)
    p.add_argument("--commission-pct", type=float, default=0.08)
    p.add_argument("--commission-min", type=float, default=1.0)
    p.add_argument("--tax-pct", type=float, default=9.0)
    p.add_argument("--pause", type=float, default=0.5, help="seconds between Yahoo requests")
    p.add_argument("--journal", default="trader/stocks-journal.json")
    p.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    try:
        return run(a) if a.cmd == "run" else stocks(a)
    except (PaperError, journal.JournalError, OSError) as e:
        print("paper: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
