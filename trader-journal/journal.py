#!/usr/bin/env python3
"""The trade journal for trader-journal. Standard library only.

It answers the one question the rest of the pack cannot: after costs, does a
setup make money? Every closed trade goes in once, measured in R (the result
divided by the risk at the stop), and the stats say what the record shows,
with honest error bars, per setup.

  add       records one closed trade: symbol, setup, side, entry, stop, exit,
            quantity, costs. Long or short. Works for shares, FX lots,
            gold and index CFDs through the instrument's point value.
  import    pairs the fills trader-broker logged in trader/state/broker.json
            (a buy, then a sell of the same stock) into closed trades, with
            the stop from the approved entry ticket. Never imports a trade twice.
  list      prints the journal, newest last.
  stats     per setup (or per symbol, or per asset class): trades, win rate
            with its 95% interval, average R before and after costs with its
            95% interval, expectancy in money, profit factor, worst run of
            losses and the deepest drawdown in R. Says plainly when the record
            does not show an edge.

    python3 journal.py add --symbol US500 --setup orb --side long --entry 5120.5 \\
        --stop 5110.5 --exit 5135.5 --qty 1 --costs 1.20 --date 2026-11-03
    python3 journal.py import --setup orb
    python3 journal.py stats
    python3 journal.py stats --by symbol --account demo --json

The journal lives in trader/journal.json. Point values and default costs per
instrument come from trader/instruments.json (see instruments.example.json).
"""

import argparse
import datetime
import hashlib
import json
import math
import os
import re
import sys

SIDES = {"long": 1, "buy": 1, "short": -1, "sell": -1}
GROUPS = ("setup", "symbol", "asset_class", "account")
Z95 = 1.959964


class JournalError(Exception):
    pass


# ---------------------------------------------------------------- files

def read_json(path, what, required=True):
    if not path or not os.path.exists(path):
        if required:
            raise JournalError("no %s at %s" % (what, path))
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as e:
        raise JournalError("%s is not valid JSON: %s" % (path, e))


def write_json(path, data):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def load_journal(path):
    doc = read_json(path, "journal", required=False) or {}
    doc.setdefault("trades", [])
    return doc


def load_instruments(path):
    doc = read_json(path, "instruments", required=False) or {}
    return {k.upper(): v for k, v in (doc.get("instruments") or {}).items()}


def _f(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _date(text, what):
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", str(text or "")):
        raise JournalError("%s wants a date like 2026-11-03" % what)
    return str(text)


# ---------------------------------------------------------------- one trade

def make_trade(symbol, setup, side, entry, stop, exit_price, qty, date, costs=None, target=None,
               point_value=None, asset_class=None, account="paper", instruments=None, note=None,
               source="manual", fills=None):
    """A closed trade, measured in money and in R. Refuses what cannot be measured."""
    sym = str(symbol or "").strip().upper()
    if not sym:
        raise JournalError("a trade needs a symbol")
    d = SIDES.get(str(side).lower())
    if d is None:
        raise JournalError("side must be long or short (buy or sell), not %r" % side)
    entry, stop, exit_price, qty = _f(entry), _f(stop), _f(exit_price), _f(qty)
    if None in (entry, stop, exit_price, qty) or entry <= 0 or exit_price <= 0 or stop <= 0 or qty <= 0:
        raise JournalError("%s: entry, stop, exit and qty must all be positive numbers" % sym)
    if (entry - stop) * d <= 0:
        raise JournalError("%s: the stop %s is on the wrong side of the entry %s for a %s trade; "
                           "without a stop below a long (above a short) there is no R"
                           % (sym, stop, entry, "long" if d > 0 else "short"))
    spec = (instruments or {}).get(sym, {})
    pv = _f(point_value) if point_value is not None else _f(spec.get("point_value", 1))
    if pv is None or pv <= 0:
        raise JournalError("%s: point_value must be positive (money per 1.0 price move per 1 unit)" % sym)
    c = _f(costs) if costs is not None else _f(spec.get("cost_per_trade", 0))
    if c is None or c < 0:
        raise JournalError("%s: costs must be zero or more, in account money" % sym)
    risk = round(abs(entry - stop) * qty * pv, 2)
    gross = round((exit_price - entry) * d * qty * pv, 2)
    net = round(gross - c, 2)
    t = {"id": None, "date": _date(date, "--date"), "symbol": sym, "setup": str(setup or "unknown"),
         "asset_class": asset_class or spec.get("asset_class") or "us_stock", "account": account,
         "side": "long" if d > 0 else "short", "qty": qty, "entry": entry, "stop": stop,
         "exit": exit_price, "point_value": pv, "risk": risk, "gross": gross, "costs": round(c, 2),
         "net": net, "r_gross": round(gross / risk, 3), "r_net": round(net / risk, 3), "source": source}
    if target is not None:
        t["target"] = _f(target)
    if note:
        t["note"] = note
    if fills:
        t["fills"] = fills
    key = fills or [t["date"], sym, t["side"], qty, entry, stop, exit_price]
    t["id"] = hashlib.sha1(json.dumps(key, sort_keys=True).encode()).hexdigest()[:12]
    return t


def add_trades(journal, trades):
    """Append the trades not already in the journal. Returns the ones added."""
    seen = {t.get("id") for t in journal["trades"]}
    added = [t for t in trades if t["id"] not in seen]
    journal["trades"].extend(added)
    return added


# ---------------------------------------------------------------- import from trader-broker

def pair_fills(state, plan=None, setup=None, instruments=None, account="paper"):
    """Closed trades from trader-broker's fill log: FIFO, long only (the pack buys, then sells).

    The stop is the one on the approved entry ticket for that stock that day;
    a round trip with no recorded stop is skipped and reported, never guessed."""
    orders = (state or {}).get("orders", [])
    stops = {}
    for rec in orders:
        if rec.get("status") == "approved" and rec.get("kind") == "entry" and _f(rec.get("stop")):
            stops[(rec.get("date"), str(rec.get("symbol", "")).upper())] = _f(rec["stop"])
    plan = plan or {}
    open_lots, trades, skipped = {}, [], []
    for i, rec in enumerate(orders):
        if rec.get("status") != "filled":
            continue
        sym = str(rec.get("symbol", "")).upper()
        qty, price = _f(rec.get("qty")), _f(rec.get("price"))
        if not sym or not qty or not price:
            continue
        if rec.get("side") == "buy":
            open_lots.setdefault(sym, []).append([qty, price, rec.get("date"), i])
            continue
        left = qty
        while left > 1e-9 and open_lots.get(sym):
            lot = open_lots[sym][0]
            take = min(lot[0], left)
            day = lot[2]
            stop = stops.get((day, sym))
            if stop is None:
                skipped.append("%s %s: no approved entry ticket with a stop that day" % (day, sym))
            else:
                st = setup or (plan.get("setup") if str(plan.get("symbol", "")).upper() == sym else None)
                spec = (instruments or {}).get(sym, {})
                share = take / qty
                cost = _f(spec.get("cost_per_trade", 0)) or 0.0
                trades.append(make_trade(sym, st, "long", lot[1], stop, price, take, day, costs=cost,
                                         account=account, instruments=instruments, source="trader-broker",
                                         fills=[lot[3], i, round(share, 6)]))
            lot[0] -= take
            left -= take
            if lot[0] <= 1e-9:
                open_lots[sym].pop(0)
    still_open = ["%s %g" % (s, sum(l[0] for l in lots)) for s, lots in open_lots.items() if lots]
    return trades, skipped, still_open


# ---------------------------------------------------------------- stats

def wilson(k, n, z=Z95):
    """95% interval for a win rate (Wilson score): honest at small n, never below 0 or above 1."""
    if n == 0:
        return (None, None)
    p = k / n
    den = 1 + z * z / n
    mid = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (round(max(0.0, mid - half), 4), round(min(1.0, mid + half), 4))


def mean_interval(xs, z=Z95):
    """Mean and its 95% interval (normal approximation). None for the interval below 2 trades."""
    n = len(xs)
    if n == 0:
        return None, (None, None)
    m = sum(xs) / n
    if n < 2:
        return round(m, 3), (None, None)
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))
    half = z * sd / math.sqrt(n)
    return round(m, 3), (round(m - half, 3), round(m + half, 3))


def drawdown(rs):
    """Deepest fall from a running peak of cumulative R, and the longest run of losses."""
    peak = cum = 0.0
    deepest = 0.0
    run = worst = 0
    for r in rs:
        cum += r
        peak = max(peak, cum)
        deepest = min(deepest, cum - peak)
        run = run + 1 if r < 0 else 0
        worst = max(worst, run)
    return round(deepest, 3), worst


def summarize(trades):
    trades = sorted(trades, key=lambda t: (t["date"], t["id"]))
    n = len(trades)
    rs = [t["r_net"] for t in trades]
    wins = sum(1 for t in trades if t["net"] > 0)
    won = sum(t["net"] for t in trades if t["net"] > 0)
    lost = -sum(t["net"] for t in trades if t["net"] < 0)
    gross = sum(t["gross"] for t in trades)
    costs = sum(t["costs"] for t in trades)
    avg_r, ci = mean_interval(rs)
    avg_r_gross, _ = mean_interval([t["r_gross"] for t in trades])
    dd, losing_run = drawdown(rs)
    s = {"trades": n, "wins": wins, "win_rate": round(wins / n, 4) if n else None,
         "win_rate_95": wilson(wins, n), "avg_r_gross": avg_r_gross, "avg_r_net": avg_r,
         "avg_r_net_95": ci, "total_r_net": round(sum(rs), 3), "net": round(sum(t["net"] for t in trades), 2),
         "expectancy": round(sum(t["net"] for t in trades) / n, 2) if n else None,
         "costs": round(costs, 2), "costs_share_of_gross": round(costs / gross, 4) if gross > 0 else None,
         "profit_factor": round(won / lost, 3) if lost > 0 else None,
         "max_drawdown_r": dd, "longest_losing_run": losing_run}
    s["verdict"] = verdict(s)
    return s


def verdict(s):
    n, lo_hi = s["trades"], s["avg_r_net_95"]
    if n == 0:
        return "no trades yet"
    if lo_hi[0] is None:
        return "one trade is not a record; keep logging"
    lo, hi = lo_hi
    if lo > 0:
        return "the record shows an edge after costs: the whole 95% interval of average R is above zero"
    if hi < 0:
        return "the record shows a loss after costs: the whole 95% interval of average R is below zero"
    return ("no edge shown yet: average R after costs could be anywhere from %+.2f to %+.2f, "
            "which includes zero. More trades narrow it; they do not promise it turns positive" % (lo, hi))


def stats(journal, by="setup", account=None, since=None):
    trades = [t for t in journal["trades"]
              if (account is None or t.get("account") == account) and (since is None or t["date"] >= since)]
    groups = {}
    for t in trades:
        groups.setdefault(str(t.get(by, "unknown")), []).append(t)
    out = {"by": by, "account": account, "since": since, "all": summarize(trades),
           "groups": {k: summarize(v) for k, v in sorted(groups.items())}}
    return out


# ---------------------------------------------------------------- printing

def _pct(x):
    return "-" if x is None else "%.0f%%" % (100 * x)


def _r(x):
    return "-" if x is None else "%+.2fR" % x


def _money(x):
    return "-" if x is None else ("-$%s" % format(-x, ",.2f") if x < 0 else "$%s" % format(x, ",.2f"))


def report(res):
    lines = []
    head = "Journal by %s" % res["by"]
    if res["account"]:
        head += ", %s account" % res["account"]
    if res["since"]:
        head += ", since %s" % res["since"]
    lines.append(head + ":")
    rows = list(res["groups"].items())
    if len(rows) > 1:
        rows.append(("ALL", res["all"]))
    for name, s in rows:
        if not s["trades"]:
            continue
        lo, hi = s["win_rate_95"]
        rlo, rhi = s["avg_r_net_95"]
        lines.append("")
        lines.append("%s: %d trade%s, %d won (%s, 95%% interval %s to %s)"
                     % (name, s["trades"], "" if s["trades"] == 1 else "s", s["wins"], _pct(s["win_rate"]),
                        _pct(lo), _pct(hi)))
        lines.append("  average R: %s before costs, %s after (95%% interval %s to %s)"
                     % (_r(s["avg_r_gross"]), _r(s["avg_r_net"]), _r(rlo), _r(rhi)))
        pf = "-" if s["profit_factor"] is None else "%.2f" % s["profit_factor"]
        lines.append("  net %s, %s a trade; costs %s%s; profit factor %s"
                     % (_money(s["net"]), _money(s["expectancy"]), _money(s["costs"]),
                        "" if s["costs_share_of_gross"] is None else
                        " (%s of the gross profit)" % _pct(s["costs_share_of_gross"]), pf))
        lines.append("  deepest drawdown %s, longest losing run %d"
                     % (_r(s["max_drawdown_r"]), s["longest_losing_run"]))
        lines.append("  verdict: " + s["verdict"])
    if not res["all"]["trades"]:
        lines.append("No trades in the journal yet. Add one with: journal.py add ... or journal.py import")
    lines.append("")
    lines.append("A record of past trades, not a forecast. Not financial advice.")
    return "\n".join(lines)


def describe(t):
    return ("%s %s %s %s %g @ %s, stop %s, out %s: %s net (%s), costs %s"
            % (t["date"], t["setup"], t["symbol"], t["side"], t["qty"], t["entry"], t["stop"], t["exit"],
               _money(t["net"]), _r(t["r_net"]), _money(t["costs"])))


# ---------------------------------------------------------------- commands

def cmd_add(a):
    journal = load_journal(a.journal)
    t = make_trade(a.symbol, a.setup, a.side, a.entry, a.stop, a.exit, a.qty,
                   a.date or datetime.date.today().isoformat(), costs=a.costs, target=a.target,
                   point_value=a.point_value, asset_class=a.asset_class, account=a.account,
                   instruments=load_instruments(a.instruments), note=a.note)
    added = add_trades(journal, [t])
    if not added:
        print("Already in the journal: %s" % describe(t))
        return 0
    write_json(a.journal, journal)
    print("Logged: %s" % describe(t))
    return 0


def cmd_import(a):
    journal = load_journal(a.journal)
    state = read_json(a.state, "broker log")
    plan = read_json(a.plan, "plan", required=False)
    trades, skipped, still_open = pair_fills(state, plan, a.setup, load_instruments(a.instruments), a.account)
    added = add_trades(journal, trades)
    if added:
        write_json(a.journal, journal)
    print("Imported %d new trade%s from %s (%d already in the journal)."
          % (len(added), "" if len(added) == 1 else "s", a.state, len(trades) - len(added)))
    for t in added:
        print("  " + describe(t))
    for s in skipped:
        print("  skipped " + s)
    if still_open:
        print("Still open, not journaled yet: %s" % ", ".join(still_open))
    return 0


def cmd_list(a):
    journal = load_journal(a.journal)
    trades = sorted(journal["trades"], key=lambda t: (t["date"], t["id"]))
    if a.json:
        print(json.dumps(trades, indent=2))
        return 0
    if not trades:
        print("The journal is empty.")
    for t in trades:
        print(describe(t))
    return 0


def cmd_stats(a):
    since = _date(a.since, "--since") if a.since else None
    res = stats(load_journal(a.journal), a.by, a.account, since)
    print(json.dumps(res, indent=2) if a.json else report(res))
    return 0


def build_parser():
    p = argparse.ArgumentParser(description="The trade journal: every closed trade in R, after costs.")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(q):
        q.add_argument("--journal", default="trader/journal.json")
        q.add_argument("--instruments", default="trader/instruments.json")

    q = sub.add_parser("add", help="record one closed trade")
    common(q)
    q.add_argument("--symbol", required=True)
    q.add_argument("--setup", default="unknown")
    q.add_argument("--side", default="long", choices=sorted(SIDES))
    q.add_argument("--entry", required=True)
    q.add_argument("--stop", required=True, help="the stop when the trade was opened (it sets 1R)")
    q.add_argument("--exit", required=True)
    q.add_argument("--qty", required=True, help="shares, lots or contracts")
    q.add_argument("--target")
    q.add_argument("--costs", help="commission + spread + financing, in account money")
    q.add_argument("--point-value", help="money per 1.0 price move per 1 unit; default from instruments, else 1")
    q.add_argument("--asset-class")
    q.add_argument("--account", default="paper", help="paper, demo or live")
    q.add_argument("--date")
    q.add_argument("--note")

    q = sub.add_parser("import", help="pair trader-broker's fills into closed trades")
    common(q)
    q.add_argument("--state", default="trader/state/broker.json")
    q.add_argument("--plan", default="trader/plan.json")
    q.add_argument("--setup")
    q.add_argument("--account", default="paper")

    q = sub.add_parser("list", help="print the journal")
    common(q)
    q.add_argument("--json", action="store_true")

    q = sub.add_parser("stats", help="what the record shows, per setup")
    common(q)
    q.add_argument("--by", default="setup", choices=GROUPS)
    q.add_argument("--account")
    q.add_argument("--since")
    q.add_argument("--json", action="store_true")
    return p


def main(argv=None):
    a = build_parser().parse_args(argv)
    try:
        return {"add": cmd_add, "import": cmd_import, "list": cmd_list, "stats": cmd_stats}[a.cmd](a)
    except JournalError as e:
        print("journal: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
