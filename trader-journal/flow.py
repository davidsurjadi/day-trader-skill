#!/usr/bin/env python3
"""The repo's whole flow, once a day at 09:50 New York, as a paper record. Standard library only.

  scanner -> news -> entry -> exit -> timemachine -> budget -> risk -> (approved) -> paper fill
  after the close: the stop, the target or flat at 15:55, read from the day's 5-minute bars

Each step is the skill's own tool, run in a working folder of its own, so the
record is what the pack itself would have done. Two steps cannot run unattended
and are replaced, as David decided on 28 Oct 2026:

  broker   every ticket that passes the risk check counts as approved (his standing
           instruction). Nothing is sent to any broker: it is a paper fill at the
           decision price, the close of the 09:45 to 09:50 bar: the first bar that
           can break the opening range (David moved the decision from 10:15 on
           28 Oct 2026, before the first session, so entries sit at the breakout).
  alerts   the stop, the target and the 15:55 flat are read from the bars after
           the close, stop first when one bar touches both (as paper.py does).

Fixed choices, so the record cannot drift:
  - candidates in the scanner's order (earliest trigger bar, then ticker), at most
    --max-candidates looked at, at most the risk step's trades a day taken;
  - news blocks a candidate only on a halt, or a share offering, in the last 18
    hours (not on dividends or buybacks); everything else it finds is kept as a tag;
  - a ticket the cash limit squeezes below half the USD 50 max loss is skipped:
    a few shares would be mostly commission;
  - the time machine never blocks: its averages are kept as tags, to be judged
    after the trial like the other splits;
  - costs: Saxo SG Classic, 0.08% an order, minimum USD 1, plus 9% GST.

  python3 flow.py morning --date 2026-10-29 --dir paper/flow \\
      --watchlist trader-scanner/watchlist.example.txt trader-scanner/watchlist-broad.txt \\
      --user-agent "Name email@example.com"
  python3 flow.py close --date 2026-10-29 --dir paper/flow --journal paper/flow-journal.json
"""

import argparse
import datetime
import json
import math
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import journal  # noqa: E402
import paper    # noqa: E402  (Yahoo bars, New York time)

SETUP = "full-flow-orb"
ACCOUNT = "paper-flow"
DECIDE_AT = "09:50"              # the first 5-minute close after the 09:30-09:45 range
FLAT_BAR = "15:50"                # flat at this bar's close, 15:55
MIN_RISK_SHARE = 0.5            # skip a ticket the cash limit squeezes below half the max loss
COSTS = {"pct": 0.08, "min": 1.0, "tax_pct": 9.0}
CONFIG = {"budget": 10000, "max_order_usd": 7500, "max_position_pct": 75, "max_loss_per_trade_usd": 50,
          "max_daily_loss_usd": 150, "max_trades_per_day": 3}


class FlowError(Exception):
    pass


def tool(rel, args, cwd, timeout=600):
    """Run one skill's tool in the working folder -> (exit code, stdout, stderr)."""
    p = subprocess.run([sys.executable, os.path.join(ROOT, rel)] + args, cwd=cwd, capture_output=True,
                       text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def tool_json(rel, args, cwd, timeout=600):
    code, out, err = tool(rel, args + ["--json"], cwd, timeout)
    try:
        return json.loads(out), None
    except ValueError:
        return None, (err.strip() or out.strip() or "exit %d" % code).splitlines()[-1][:200]


def read(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def write(path, data):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1)
        fh.write("\n")


def news_blocks(news, symbol):
    """What blocks a ticker in its news items (a halt, or a share offering), and how many items there were."""
    items = [i for i in (news or {}).get("items", []) if symbol in (i.get("touches") or [i.get("ticker")])]
    hits = set()
    for i in items:
        tags = i.get("tags") or []
        text = ("%s %s" % (i.get("title") or "", i.get("summary") or "")).lower()
        if "halt" in tags:
            hits.add("halt")
        if "capital" in tags and re.search(r"\boffering\b", text):
            hits.add("offering")
    return sorted(hits), len(items)


def round_trip_cost(qty, entry, exit_price):
    side = lambda price: max(COSTS["min"], COSTS["pct"] / 100.0 * qty * price)
    return round((side(entry) + side(exit_price)) * (1 + COSTS["tax_pct"] / 100.0), 2)


def simulate(bars, entry, stop, target, after):
    """Bars keyed "HH:MM" -> (exit price, outcome, bar): stop first when one bar touches both."""
    slots = sorted(s for s in bars if after <= s <= FLAT_BAR)
    for s in slots:
        b = bars[s]
        if b["l"] <= stop:
            return stop, "stop", s
        if b["h"] >= target:
            return target, "target", s
    if FLAT_BAR not in bars:
        raise FlowError("the session is not complete (no %s bar)" % FLAT_BAR)
    return round(bars[FLAT_BAR]["c"], 2), "flat", FLAT_BAR


# ---------------------------------------------------------------- morning

def morning(a):
    day = a.date
    work = os.path.join(a.dir, day, "work")
    out_path = os.path.join(a.dir, day, "tickets.json")
    if os.path.exists(out_path):
        print("%s: tickets already decided (%s); nothing re-run." % (day, out_path))
        return 0
    if os.path.exists(work):
        shutil.rmtree(work)
    os.makedirs(os.path.join(work, "trader", "state"))
    cfg = read(os.path.join(ROOT, "trader-budget", "config.example.json"))
    cfg.update(CONFIG, sec_user_agent=a.user_agent or cfg.get("sec_user_agent"))
    write(os.path.join(work, "trader", "config.json"), cfg)
    watch = os.path.join(work, "universe.txt")
    syms = []
    for p in a.watchlist:
        with open(p, encoding="utf-8") as fh:
            syms += [ln.strip().upper() for ln in fh if ln.strip() and not ln.startswith("#")]
    with open(watch, "w") as fh:
        fh.write("\n".join(dict.fromkeys(syms)) + "\n")
    now = "%s %s" % (day, DECIDE_AT)

    code, out, err = tool("trader-scanner/scan.py", ["orb", "--watchlist", "universe.txt", "--at", DECIDE_AT,
                                                     "--now", now, "--quiet"], work, timeout=1200)
    hits = (read(os.path.join(work, "trader", "watchlist.json"), {}) or {}).get("hits", [])
    if code != 0 and not hits:
        raise FlowError("the scanner failed: %s" % (err.strip() or out.strip())[-200:])

    state = {"orders": []}
    portfolio = {"cash": float(cfg["budget"]), "positions": []}
    prices = {}
    taken, looked = [], []
    for rank, h in enumerate(hits[:a.max_candidates], 1):
        if len(taken) >= cfg["max_trades_per_day"]:
            break
        sym = h["symbol"]
        row = {"rank": rank, "symbol": sym, "trigger": h.get("at"), "or_high": h.get("or_high"),
               "or_low": h.get("or_low")}
        looked.append(row)
        news, why = tool_json("trader-news/news.py", ["--tickers", sym, "--hours", "18", "--dry-run",
                                                      "--now", paper_now_utc(day), "--user-agent",
                                                      cfg["sec_user_agent"]], work)
        blocks, n_items = news_blocks(news, sym)
        row.update(news_items=n_items, news_tags=sorted({t for i in (news or {}).get("items", []) for t in i.get("tags") or []}),
                   news_error=why)
        if blocks:
            row.update(decision="blocked", reason="news: %s" % ", ".join(blocks))
            continue
        ent, why = tool_json("trader-entry/entry.py", ["check", sym, "--now", now, "--write"], work)
        if not ent or ent.get("status") != "triggered":
            row.update(decision="skipped", reason="entry: %s" % ((ent or {}).get("reason") or why))
            continue
        price = round(float((ent.get("last") or {}).get("close") or 0), 2)
        row["entry"] = price
        plan, why = tool_json("trader-exit/exit.py", ["plan", "--now", now, "--entry", str(price), "--time",
                                                      clock_label(DECIDE_AT), "--write"], work)
        if not plan or not plan.get("stop") or plan["stop"] >= price:
            row.update(decision="skipped", reason="exit: %s" % (why or "the price is back below the stop"))
            continue
        row.update(stop=plan["stop"], target=plan["target"])
        tm, why = tool_json("trader-timemachine/timemachine.py", ["run", "--now", now, "--write"], work,
                            timeout=900)
        if tm:
            mc, hist = tm.get("mc") or {}, tm.get("history") or {}
            row.update(tm_avg_r=mc.get("avg_R"), tm_target_first=mc.get("p_target"),
                       tm_stop_first=mc.get("p_stop"), tm_hist_avg_r=hist.get("avg_R"),
                       tm_hist_n=hist.get("triggered"), tm_verdict=tm.get("verdict"))
        else:
            row["tm_error"] = why
        prices[sym] = price
        write(os.path.join(work, "trader", "prices.json"), prices)
        write(os.path.join(work, "trader", "portfolio.json"), portfolio)
        write(os.path.join(work, "trader", "state", "broker.json"), state)
        code, out, err = tool("trader-budget/size.py", ["--now", now, "--prices", "trader/prices.json",
                                                        "--portfolio", "trader/portfolio.json", "--write"], work)
        orders = (read(os.path.join(work, "trader", "orders.json"), {}) or {}).get("orders", [])
        if code != 0 or not orders:
            last = [ln for ln in (err.strip() or out.strip()).splitlines() if "No order" in ln or "cash" in ln]
            row.update(decision="skipped", reason="budget: %s" % " ".join((last[0] if last else "no order fits").split())[:160])
            continue
        risk, why = tool_json("trader-risk/check.py", ["--now", now, "--today", day, "--no-write"], work)
        res = ((risk or {}).get("results") or [{}])[0]
        qty = int(float(str(orders[0].get("qty") or orders[0].get("quantity") or 0)))
        row.update(qty=qty, max_loss=res.get("max_loss"), risk_verdict=res.get("verdict"),
                   risk_reasons=res.get("reasons"))
        if res.get("verdict") != "PASS":
            row.update(decision="blocked", reason="risk: %s" % "; ".join(res.get("reasons") or [why or "no verdict"]))
            continue
        if (res.get("max_loss") or 0) < MIN_RISK_SHARE * cfg["max_loss_per_trade_usd"]:
            row.update(decision="skipped", reason="budget: only %s shares fit the cash left (USD %.2f at the stop)"
                       % (qty, res.get("max_loss") or 0))
            continue
        row.update(decision="taken", approval="standing instruction (paper), not a typed yes")
        taken.append(row)
        state["orders"].append({"symbol": sym, "side": "buy", "kind": "entry", "status": "placed", "date": day,
                                "at": now, "max_loss": res.get("max_loss"), "note": "paper flow, no broker"})
        portfolio["cash"] = round(portfolio["cash"] - qty * price, 2)
        portfolio["positions"].append({"symbol": sym, "qty": qty})
    write(out_path, {"date": day, "decided_at": now, "hits": len(hits), "looked": looked,
                     "taken": [r["symbol"] for r in taken], "config": CONFIG, "costs": COSTS})
    for r in looked:
        print("%s %s #%d %s: %s%s" % (day, r["symbol"], r["rank"], r.get("decision", "?"),
                                     r.get("reason") or "entry %.2f, stop %.2f, target %.2f, %s shares" % (
                                         r["entry"], r["stop"], r["target"], r.get("qty")),
                                     "" if r.get("tm_hist_avg_r") is None else
                                     " (time machine: history %+.2fR over %s)" % (r["tm_hist_avg_r"], r.get("tm_hist_n"))))
    print("%s: %d breakouts by %s, %d looked at, %d taken: %s" % (day, len(hits), DECIDE_AT, len(looked),
                                                                 len(taken), ", ".join(r["symbol"] for r in taken) or "none"))
    return 0


def clock_label(hm):
    """ "09:50" -> "9:50 AM", the form trader-exit takes for --time."""
    hh, mm = map(int, hm.split(":"))
    return "%d:%02d %s" % (hh % 12 or 12, mm, "AM" if hh < 12 else "PM")


def paper_now_utc(day):
    hh, mm = map(int, DECIDE_AT.split(":"))
    local = datetime.datetime.combine(datetime.date.fromisoformat(day), datetime.time(hh, mm))
    utc = local - datetime.timedelta(hours=paper.eastern_offset(local + datetime.timedelta(hours=5)))
    return utc.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- close

def close(a):
    day = a.date
    doc = read(os.path.join(a.dir, day, "tickets.json"))
    if not doc:
        print("%s: no tickets were decided this morning. Nothing logged." % day)
        return 0
    rows = [r for r in doc["looked"] if r.get("decision") == "taken"]
    if not rows:
        print("%s: no ticket passed the risk check. Nothing logged." % day)
        return 0
    jdoc = journal.load_journal(a.journal)
    trades = []
    for r in rows:
        bars = paper.session_bars(paper.fetch_yahoo(r["symbol"]), day)
        exit_price, how, at = simulate(bars, r["entry"], r["stop"], r["target"], DECIDE_AT)
        cost = round_trip_cost(r["qty"], r["entry"], exit_price)
        t = journal.make_trade(r["symbol"], SETUP, "long", r["entry"], r["stop"], exit_price, r["qty"], day,
                               costs=cost, target=r["target"], point_value=1, asset_class="us_stock",
                               account=ACCOUNT, source="flow.py",
                               note="rank %d; entry %s at the %s decision; %s at %s; approved by standing "
                                    "instruction (paper)" % (r["rank"], r["entry"], DECIDE_AT, how, at))
        t["tags"] = {k: r.get(k) for k in ("rank", "tm_avg_r", "tm_hist_avg_r", "tm_hist_n", "tm_target_first",
                                          "tm_stop_first", "news_items", "news_tags")}
        trades.append(t)
        print("%s %s: entry %.2f, stop %.2f, target %.2f -> %s at %s, exit %.2f, %d shares: %+.2fR after USD %.2f costs"
              % (day, r["symbol"], r["entry"], r["stop"], r["target"], how, at, exit_price, r["qty"], t["r_net"], cost))
    added = journal.add_trades(jdoc, trades)
    journal.write_json(a.journal, jdoc)
    print("%s: logged %d new trade(s) to %s" % (day, len(added), a.journal))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("morning")
    m.add_argument("--date", required=True)
    m.add_argument("--dir", default="paper/flow")
    m.add_argument("--watchlist", nargs="+", required=True)
    m.add_argument("--max-candidates", type=int, default=12)
    m.add_argument("--user-agent", help='SEC contact for the news step: "Name email@example.com"')
    c = sub.add_parser("close")
    c.add_argument("--date", required=True)
    c.add_argument("--dir", default="paper/flow")
    c.add_argument("--journal", default="paper/flow-journal.json")
    a = ap.parse_args(argv)
    try:
        datetime.date.fromisoformat(a.date)
        return morning(a) if a.cmd == "morning" else close(a)
    except (FlowError, paper.PaperError, journal.JournalError, ValueError) as e:
        print("flow: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
