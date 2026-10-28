#!/usr/bin/env python3
"""Build the paper trial's dashboard data from its journals. Standard library only.

Reads the paper journals (US500, NAS100, the AI and broad stock baskets), keeps a
session log so no-trade sessions stay in the history, and writes four JSON files of
row objects for the dashboard:

  records.json   one row per session per main record, with that record as it stood
                 after the session (the history the verdicts are read from)
  splits.json    the pre-registered splits, as they stand after the latest session
  trades.json    every trade, with its tags, dollars and cost comparisons
  sessions.json  one row per logged session: when the run finished and any note

The records and splits are fixed for the whole trial (decided 28 Oct 2026, before the
first trial session) so the grouping cannot drift toward whatever looked good:

  us500, nas100  the index CFDs; dollars shown at USD 50 risk a trade (one unit is logged)
  stocks         the AI 18 and broad 289 pooled, judged by session totals: same-day trades
                 move together, so sessions, not trades, are the independent evidence
  small5k        a USD 5,000 account: only the first stock signal each session
  small10k       a USD 10,000 account: only the first two (by entry bar, then ticker)

  US500 by day type        or_width_ratio >= 1 "volatile", below 1 "quiet"
  stocks by rvol           rvol >= 1 "in play", below 1 "not in play"
  stocks by stop distance  under 0.5%, 0.5% to 1%, 1% or more
  stocks by theme          AI 18, other 289

The moomoo column re-prices each stock trade with moomoo SG's published US fees as of
28 Oct 2026 (USD 0 commission for eligible clients, USD 0.99 platform fee an order plus
9% GST, settlement USD 0.003 a share up to 1% of the amount, SEC and FINRA sell fees,
CAT fee). It is a comparison only: the trial's record uses Saxo's costs.

  python3 trial_dashboard.py --journals paper --sessions paper/sessions-log.json \
      --date 2026-10-28 --out /tmp/dash
"""
import argparse
import datetime
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import journal as J  # noqa: E402  (the same statistics journal.py prints)

SGT = datetime.timezone(datetime.timedelta(hours=8), "SGT")   # Singapore keeps no summer time
INDEX = [("us500", "US500", "us500-journal.json"), ("nas100", "NAS100", "nas100-journal.json")]
BASKETS = [("ai18", "AI 18", "stocks-journal.json"), ("other289", "Other 289", "broad-journal.json")]
INDEX_RISK_USD = 50.0
SMALL = [("small5k", "USD 5,000 account", 1), ("small10k", "USD 10,000 account", 2)]
NAMES = {"us500": "US500", "nas100": "NAS100", "stocks": "US stocks, 307",
         "small5k": "USD 5,000 account", "small10k": "USD 10,000 account"}


def load(path):
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [t for t in json.load(f).get("trades", []) if t.get("account") == "paper"]


def moomoo_costs(t):
    """Round-trip USD cost of one whole-share stock trade at moomoo SG's published fees."""
    q, buy, sell = t["qty"], t["qty"] * t["entry"], t["qty"] * t["exit"]
    platform = 2 * 0.99 * 1.09
    settle = min(0.003 * q, 0.01 * buy) + min(0.003 * q, 0.01 * sell)
    sec = max(0.01, 0.0000206 * sell)
    taf = min(9.79, max(0.01, 0.000195 * q))
    cat = 2 * 0.000003 * q
    return round(platform + settle + sec + taf + cat, 2)


def status_of(n, lo, hi):
    if n == 0:
        return "none"
    if lo is None:
        return "few"
    return "edge" if lo > 0 else "loss" if hi < 0 else "open"


def verdict_of(status, lo, hi, unit):
    return {"none": "no trades yet",
            "few": "one %s is not a record; keep logging" % unit,
            "edge": "the record shows an edge after costs: the whole 95%% interval of average R per %s is above zero" % unit,
            "loss": "the record shows a loss after costs: the whole 95%% interval of average R per %s is below zero" % unit,
            }.get(status) or ("no edge shown yet: average R after costs per %s could be anywhere from %+.2f to %+.2f, "
                              "which includes zero. More %ss narrow it; they do not promise it turns positive"
                              % (unit, lo, hi, unit))


def first_signals(trades, n):
    """The first n stock trades of each session, by entry bar then ticker: what a small account takes."""
    by_day = {}
    for t in sorted(trades, key=lambda t: (t["date"], t["tags"].get("entry_time") or "99:99", t["symbol"])):
        by_day.setdefault(t["date"], [])
        if len(by_day[t["date"]]) < n:
            by_day[t["date"]].append(t)
    return [t for day in by_day.values() for t in day]


def record_rows(rid, trades, dates, usd_of, by_session):
    """One row per session: that day's trades and the record as it stood after it."""
    out = []
    for d in dates:
        today = [t for t in trades if t["date"] == d]
        upto = [t for t in trades if t["date"] <= d]
        s = J.summarize(upto)
        wlo, whi = s["win_rate_95"]
        row = {"date": d, "record": rid, "record_name": NAMES[rid],
               "day_trades": len(today), "day_wins": sum(1 for t in today if t["net"] > 0),
               "day_r": round(sum(t["r_net"] for t in today), 3),
               "day_usd": round(sum(usd_of(t) for t in today), 2),
               "trades": s["trades"], "wins": s["wins"], "win_rate": s["win_rate"], "win_lo": wlo, "win_hi": whi,
               "avg_r": s["avg_r_net"], "trade_lo": s["avg_r_net_95"][0], "trade_hi": s["avg_r_net_95"][1],
               "total_r": s["total_r_net"], "total_usd": round(sum(usd_of(t) for t in upto), 2),
               "costs_share": s["costs_share_of_gross"], "max_drawdown_r": s["max_drawdown_r"]}
        if by_session:
            days = [x for x in dates if x <= d]
            per = [round(sum(t["r_net"] for t in upto if t["date"] == x), 3) for x in days]
            m, (lo, hi) = J.mean_interval(per)
            row.update({"basis": "session", "sessions": len(days), "sessions_positive": sum(1 for x in per if x > 0),
                        "avg_basis": m, "lo": lo, "hi": hi})
            row["status"] = status_of(s["trades"], lo, hi) if s["trades"] else "none"
            row["verdict"] = verdict_of(row["status"], lo, hi, "session")
        else:
            lo, hi = s["avg_r_net_95"]
            row.update({"basis": "trade", "sessions": len([x for x in dates if x <= d]),
                        "sessions_positive": None, "avg_basis": s["avg_r_net"], "lo": lo, "hi": hi})
            row["status"] = status_of(s["trades"], lo, hi)
            row["verdict"] = s["verdict"]
        out.append(row)
    return out


def split_row(split, group, record, trades, dates, label):
    s = J.summarize(trades)
    lo, hi = s["avg_r_net_95"]
    traded = sorted({t["date"] for t in trades})
    pos = sum(1 for d in traded if sum(t["r_net"] for t in trades if t["date"] == d) > 0)
    cost_r = [t["r_gross"] - t["r_net"] for t in trades]
    return {"key": split + ":" + group, "split": split, "group": group, "label": label, "record": record,
            "trades": s["trades"], "sessions_traded": len(traded), "sessions_positive": pos,
            "avg_r": s["avg_r_net"], "lo": lo, "hi": hi, "status": status_of(s["trades"], lo, hi),
            "avg_cost_r": round(sum(cost_r) / len(cost_r), 3) if cost_r else None,
            "usd": round(sum(t["usd"] for t in trades), 2)}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--journals", required=True, help="folder holding the journal files")
    p.add_argument("--sessions", required=True, help="the session log to read and update")
    p.add_argument("--date", help="the session just logged, YYYY-MM-DD; omit to rebuild only")
    p.add_argument("--note", default="", help="a short note for this session, e.g. symbols that failed")
    p.add_argument("--out", required=True)
    a = p.parse_args()

    log = {"sessions": []}
    if os.path.exists(a.sessions):
        with open(a.sessions) as f:
            log = json.load(f)
    if a.date:
        datetime.date.fromisoformat(a.date)
        row = {"date": a.date, "completed_at": datetime.datetime.now(SGT).isoformat(timespec="seconds"), "note": a.note}
        log["sessions"] = sorted([s for s in log["sessions"] if s["date"] != a.date] + [row], key=lambda s: s["date"])
        with open(a.sessions, "w") as f:
            json.dump(log, f, indent=1)
            f.write("\n")
    dates = [s["date"] for s in log["sessions"]]

    trades_out, records, splits = [], [], []
    for rid, name, fname in INDEX:
        ts = sorted(load(os.path.join(a.journals, fname)), key=lambda t: (t["date"], t["id"]))
        for t in ts:
            t["usd"] = round(t["r_net"] * INDEX_RISK_USD, 2)
            t["record"], t["basket"] = rid, rid
        records += record_rows(rid, ts, dates, lambda t: t["usd"], by_session=False)
        trades_out += ts
        if rid == "us500":
            for g, label, f in (("volatile", "Volatile days", lambda r: r >= 1), ("quiet", "Quiet days", lambda r: r < 1)):
                sel = [t for t in ts if t.get("tags", {}).get("or_width_ratio") is not None and f(t["tags"]["or_width_ratio"])]
                splits.append(split_row("us500_day", g, "us500", sel, dates, label))
            sel = [t for t in ts if t.get("tags", {}).get("or_width_ratio") is None]
            if sel:
                splits.append(split_row("us500_day", "untagged", "us500", sel, dates, "No day type (logged before tags)"))

    stocks = []
    for bid, name, fname in BASKETS:
        for t in load(os.path.join(a.journals, fname)):
            t.setdefault("tags", {})
            t["usd"] = t["net"]
            t["record"], t["basket"], t["basket_name"] = "stocks", bid, name
            t["costs_moomoo"] = moomoo_costs(t)
            t["r_net_moomoo"] = round((t["gross"] - t["costs_moomoo"]) / t["risk"], 3)
            stocks.append(t)
    stocks.sort(key=lambda t: (t["date"], t["id"]))
    records += record_rows("stocks", stocks, dates, lambda t: t["usd"], by_session=True)
    for rid, name, n in SMALL:
        picked = first_signals(stocks, n)
        ids = {t["id"] for t in picked}
        for t in stocks:
            t["in_" + rid] = t["id"] in ids
        records += record_rows(rid, picked, dates, lambda t: t["usd"], by_session=False)
    trades_out += stocks

    def tagged(key, f):
        return [t for t in stocks if t["tags"].get(key) is not None and f(t["tags"][key])]
    for g, label, f in (("in", "In play (RVOL 1 or more)", lambda r: r >= 1), ("out", "Not in play (RVOL under 1)", lambda r: r < 1)):
        splits.append(split_row("rvol", g, "stocks", tagged("rvol", f), dates, label))
    untagged = [t for t in stocks if t["tags"].get("rvol") is None]
    if untagged:
        splits.append(split_row("rvol", "untagged", "stocks", untagged, dates, "No RVOL (logged before tags)"))
    stop_pct = lambda t: (t["entry"] - t["stop"]) / t["entry"] * 100
    for g, label, lo_, hi_ in (("tight", "Stop under 0.5%", 0, 0.5), ("mid", "Stop 0.5% to 1%", 0.5, 1.0), ("wide", "Stop 1% or more", 1.0, math.inf)):
        splits.append(split_row("stop", g, "stocks", [t for t in stocks if lo_ <= stop_pct(t) < hi_], dates, label))
    for bid, name, _ in BASKETS:
        splits.append(split_row("theme", bid, "stocks", [t for t in stocks if t["basket"] == bid], dates, name))

    costs = []
    for rid, sel in (("us500", [t for t in trades_out if t["record"] == "us500"]),
                     ("nas100", [t for t in trades_out if t["record"] == "nas100"]), ("stocks", stocks)):
        s = J.summarize(sel)
        row = {"record": rid, "record_name": NAMES[rid], "trades": s["trades"], "costs_share": s["costs_share_of_gross"],
               "avg_cost_r": round(sum(t["r_gross"] - t["r_net"] for t in sel) / len(sel), 3) if sel else None,
               "avg_r": s["avg_r_net"], "avg_cost_r_moomoo": None, "avg_r_moomoo": None}
        if rid == "stocks" and sel:
            row["avg_cost_r_moomoo"] = round(sum(t["r_gross"] - t["r_net_moomoo"] for t in sel) / len(sel), 3)
            row["avg_r_moomoo"] = round(sum(t["r_net_moomoo"] for t in sel) / len(sel), 3)
        costs.append(row)

    keep = ("id", "record", "basket", "basket_name", "date", "symbol", "qty", "entry", "stop", "exit", "risk",
            "r_gross", "r_net", "costs", "net", "usd", "costs_moomoo", "r_net_moomoo", "in_small5k", "in_small10k")
    rows = []
    for t in sorted(trades_out, key=lambda t: (t["date"], t["record"], t["symbol"])):
        r = {k: t.get(k) for k in keep}
        tags = t.get("tags", {})
        r.update({"entry_time": tags.get("entry_time"), "exit_by": tags.get("exit_by"),
                  "stop_pct": round(stop_pct(t), 3), "rvol": tags.get("rvol"), "or_width_ratio": tags.get("or_width_ratio")})
        rows.append(r)

    os.makedirs(a.out, exist_ok=True)
    for fname, data in (("records.json", records), ("splits.json", splits), ("costs.json", costs),
                        ("trades.json", rows), ("sessions.json", log["sessions"])):
        with open(os.path.join(a.out, fname), "w") as f:
            json.dump(data, f)
    print("%d sessions, %d trades, %d record rows, %d split rows -> %s" % (len(dates), len(rows), len(records), len(splits), a.out))


if __name__ == "__main__":
    main()
