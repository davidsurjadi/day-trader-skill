#!/usr/bin/env python3
"""Saxo demo pilot: the opening-range rule on US index CFDs, as real orders on the
Saxo SIMULATION account. Standard library only.

This is the one file in trader-saxo that sends orders. It talks to Saxo's
simulation gateway only: the base URL is fixed to SIM and checked before every
write, so it cannot reach a live account. It trades the three US index CFDs
(US500.I, USNAS100.I, US30.I), never stocks.

  plan     the 15-minute opening range from Saxo's own 1-minute bars, and the
           orders the rule would place (prints; sends nothing)
  place    plan, check every order with Saxo's precheck, then place a buy-stop
           at the range high with its stop (range midpoint) and 2R target
           attached; saves the order ids to the day's state file
  flatten  at 15:55 New York: cancel this pilot's working orders and close any
           open position in the three CFDs at market
  record   read the day's closed positions back from Saxo and log each one to
           the journal (account "saxo-demo"), with the fill against the plan

    python3 demo.py plan --date 2026-10-29
    python3 demo.py place --date 2026-10-29 --state paper/saxo-demo/2026-10-29.json
    python3 demo.py flatten --date 2026-10-29 --state paper/saxo-demo/2026-10-29.json
    python3 demo.py record --date 2026-10-29 --state paper/saxo-demo/2026-10-29.json \
        --journal paper/saxo-demo-journal.json

Sizing: risk at the stop is --risk (USD 50) on a USD 10,000 capital that this
file enforces itself (the demo account holds far more, in EUR). One contract of
these CFDs moves USD 1 per index point. The orders' total initial margin must
stay within --margin-share (half) of the capital, or the order is skipped.

The rule differs from paper.py on purpose: the buy-stop fills when the price
touches the range high, not on a 5-minute close above it. Keep its record apart.
"""

import argparse
import datetime
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import saxo  # noqa: E402  (the read-only client next to this file)

sys.path.insert(0, os.path.join(os.path.dirname(HERE), "trader-journal"))
try:
    import journal  # noqa: E402  (trader-journal, for the record step)
except ImportError:  # the folder copied on its own: plan, place and flatten still work
    journal = None

INDICES = {"US500": (4913, "US500.I"), "NAS100": (4912, "USNAS100.I"), "US30": (4911, "US30.I")}
TICKS = {4913: 0.01, 4912: 0.01, 4911: 0.1}    # TickSize from Saxo's instrument details, 28 Oct 2026
ASSET = "CfdOnIndex"
RULE = {"name": "orb15-mid-2R-saxo-stop", "or_minutes": 15, "target_r": 2.0, "flat": "15:55"}
AMOUNT_STEP = 0.1      # AmountDecimals 1
NYSE_HOLIDAYS = {
    "2026-01-01": "New Year's Day",
    "2026-01-19": "Martin Luther King, Jr. Day",
    "2026-02-16": "Washington's Birthday",
    "2026-04-03": "Good Friday",
    "2026-05-25": "Memorial Day",
    "2026-06-19": "Juneteenth National Independence Day",
    "2026-07-03": "Independence Day (observed)",
    "2026-09-07": "Labor Day",
    "2026-11-26": "Thanksgiving Day",
    "2026-12-25": "Christmas Day",
    "2027-01-01": "New Year's Day",
    "2027-01-18": "Martin Luther King, Jr. Day",
    "2027-02-15": "Washington's Birthday",
    "2027-03-26": "Good Friday",
    "2027-05-31": "Memorial Day",
    "2027-06-18": "Juneteenth National Independence Day (observed)",
    "2027-07-05": "Independence Day (observed)",
    "2027-09-06": "Labor Day",
    "2027-11-25": "Thanksgiving Day",
    "2027-12-24": "Christmas Day (observed)",
}
NYSE_EARLY_CLOSE = {  # 1:00 p.m. ET: the pilot does not trade these days
    "2026-11-27": "day after Thanksgiving",
    "2026-12-24": "Christmas Eve",
    "2027-11-26": "day after Thanksgiving",
}
CALENDAR_YEARS = (2026, 2027)


class DemoError(Exception):
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


def range_end_utc(day):
    """09:45 New York on this date, as Saxo's chart Time (UTC): the last bar asked for."""
    local = datetime.datetime.combine(datetime.date.fromisoformat(day), datetime.time(9, 45))
    guess = local - datetime.timedelta(hours=-5)
    utc = local - datetime.timedelta(hours=eastern_offset(guess))
    return utc.strftime("%Y-%m-%dT%H:%M:%SZ")


def no_session(day):
    """Why the pilot does not trade this New York date, or None."""
    d = datetime.date.fromisoformat(day)
    if d.year not in CALENDAR_YEARS:
        return "the holiday list covers %s only" % " and ".join(map(str, CALENDAR_YEARS))
    if d.weekday() >= 5:
        return "a weekend"
    if day in NYSE_HOLIDAYS:
        return "an NYSE holiday (%s)" % NYSE_HOLIDAYS[day]
    if day in NYSE_EARLY_CLOSE:
        return "an early close (%s)" % NYSE_EARLY_CLOSE[day]
    return None


# ---------------------------------------------------------------- the rule (pure)

def opening_range(chart, day):
    """Saxo 1-minute bars -> (high, low) of the 09:30-09:44 New York bars, on the bid/ask midpoint."""
    hi, lo, seen = None, None, set()
    for row in chart.get("Data", []):
        if not row.get("Time"):
            continue
        ny = to_eastern(datetime.datetime(1970, 1, 1) + datetime.timedelta(seconds=saxo._epoch(row["Time"])))
        if ny.date().isoformat() != day or not "09:30" <= ny.strftime("%H:%M") <= "09:44":
            continue
        _, h, l, _ = saxo._ohlc(row)
        if h is None or l is None:
            continue
        seen.add(ny.strftime("%H:%M"))
        hi = h if hi is None else max(hi, h)
        lo = l if lo is None else min(lo, l)
    if len(seen) < RULE["or_minutes"]:
        raise DemoError("the opening range is incomplete (%d of %d one-minute bars)" % (len(seen), RULE["or_minutes"]))
    return hi, lo


def plan_trade(high, low, risk_usd, tick=0.01):
    """The orders the rule places for one index: a buy-stop one tick above the range high,
    the stop at the range midpoint, the target at 2R, sized so the stop loses at most risk_usd.
    Prices sit on the instrument's tick (Saxo refuses any other)."""
    dp = max(0, -int(math.floor(math.log10(tick) + 1e-9)))
    entry = round(math.ceil(round((high + tick) / tick, 6)) * tick, dp)
    stop = round(math.floor(round(((high + low) / 2) / tick, 6)) * tick, dp)
    if entry - stop <= 0:
        raise DemoError("the opening range has no width")
    target = round(math.floor(round((entry + RULE["target_r"] * (entry - stop)) / tick, 6)) * tick, dp)
    amount = math.floor(round(risk_usd / (entry - stop) / AMOUNT_STEP, 6)) * AMOUNT_STEP
    amount = round(amount, 1)
    return {"or_high": round(high, 2), "or_low": round(low, 2), "entry": entry, "stop": stop, "target": target,
            "amount": amount, "risk_usd": round(amount * (entry - stop), 2)}


def order_body(account_key, uic, p):
    """Entry buy-stop (today only) with its stop and target attached (they guard the position once filled)."""
    def leg(buy_sell, order_type, price, duration):
        return {"AccountKey": account_key, "Uic": uic, "AssetType": ASSET, "BuySell": buy_sell, "Amount": p["amount"],
                "OrderType": order_type, "OrderPrice": price, "ManualOrder": False,
                "OrderDuration": {"DurationType": duration}}
    body = leg("Buy", "StopIfTraded", p["entry"], "DayOrder")
    body["Orders"] = [leg("Sell", "StopIfTraded", p["stop"], "GoodTillCancel"),
                      leg("Sell", "Limit", p["target"], "GoodTillCancel")]
    return body


# ---------------------------------------------------------------- transport (SIM only)

class Trader(saxo.Client):
    """The read-only client plus POST and DELETE, refused anywhere but Saxo's simulation gateway."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        if self.base != saxo.SIM:
            raise DemoError("demo.py only talks to Saxo's simulation gateway %s" % saxo.SIM)

    write_pause = 1.5

    def send(self, method, path, body=None, params=None, retry=True):
        if self.base != saxo.SIM:
            raise DemoError("refusing to send an order anywhere but %s" % saxo.SIM)
        q = {k: v for k, v in (params or {}).items() if v is not None}
        url = self.base + path + ("?" + urllib.parse.urlencode(q) if q else "")
        headers = {"Accept": "application/json", "Content-Type": "application/json", "User-Agent": saxo.UA}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        time.sleep(self.write_pause)       # Saxo throttles order requests per session
        try:
            with self.opener(req, timeout=30) as r:
                text = r.read().decode("utf-8")
                return json.loads(text) if text else {}
        except urllib.error.HTTPError as e:
            if e.code == 429 and retry:
                time.sleep(5)
                return self.send(method, path, body, params, retry=False)
            if e.code == 401:
                raise DemoError("Saxo said 401: the 24-hour token is missing or expired. Get a new one at "
                                "https://www.developer.saxo/openapi/token and update the network secret.")
            detail = ""
            try:
                detail = e.read().decode("utf-8")[:400]
            except Exception:
                pass
            raise DemoError("Saxo returned HTTP %d for %s %s %s" % (e.code, method, path, detail))
        except urllib.error.URLError as e:
            raise DemoError("could not reach Saxo: %s" % e.reason)


def account_key(c):
    data = c.get("/port/v1/accounts/me").get("Data", [])
    if len(data) != 1:
        raise DemoError("expected one demo account, Saxo lists %d" % len(data))
    if not data[0].get("IsTrialAccount"):
        raise DemoError("the account is not a trial (demo) account: refusing to trade")
    return data[0]["AccountKey"]


def _rows(d):
    return d if isinstance(d, list) else (d or {}).get("Data", [])


# ---------------------------------------------------------------- state

def read_state(path):
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return {}


def write_state(path, state):
    if not path:
        return
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=1, sort_keys=True)
        fh.write("\n")


# ---------------------------------------------------------------- commands

def make_plans(c, day, risk):
    plans = {}
    for name, (uic, symbol) in INDICES.items():
        try:
            hi, lo = opening_range(c.get("/chart/v3/charts", {"Uic": uic, "AssetType": ASSET, "Horizon": 1,
                                                               "Count": 60, "Mode": "UpTo",
                                                               "Time": range_end_utc(day)}), day)
            p = plan_trade(hi, lo, risk, TICKS[uic])
            if p["amount"] <= 0:
                raise DemoError("the stop is too far for USD %s risk at the 0.1-contract minimum" % risk)
            plans[name] = dict(p, uic=uic, symbol=symbol)
        except (DemoError, saxo.SaxoError) as e:
            plans[name] = {"uic": uic, "symbol": symbol, "skip": str(e)}
    return plans


def cmd_plan(a, c):
    why = no_session(a.date)
    if why:
        print("%s: no trading, %s." % (a.date, why))
        return 0
    for name, p in make_plans(c, a.date, a.risk).items():
        if "skip" in p:
            print("%s %s: skip, %s" % (name, a.date, p["skip"]))
        else:
            print("%s %s: range %.2f-%.2f -> buy-stop %.2f, stop %.2f, target %.2f, %.1f contracts, USD %.2f at risk"
                  % (name, a.date, p["or_low"], p["or_high"], p["entry"], p["stop"], p["target"], p["amount"],
                     p["risk_usd"]))
    return 0


def cmd_place(a, c):
    why = no_session(a.date)
    if why:
        print("%s: no orders, %s." % (a.date, why))
        return 0
    state = read_state(a.state)
    if state.get("placed"):
        print("%s: orders already placed for this date (see %s); nothing sent." % (a.date, a.state))
        return 0
    acc = account_key(c)
    plans = make_plans(c, a.date, a.risk)
    budget_usd = a.capital * a.margin_share
    used_usd = 0.0
    for name, p in plans.items():
        if "skip" in p:
            print("%s %s: skip, %s" % (name, a.date, p["skip"]))
            continue
        body = order_body(acc, p["uic"], p)
        chk = c.send("POST", "/trade/v2/orders/precheck", dict(body, FieldGroups=["Costs", "MarginImpactBuySell"]))
        if chk.get("PreCheckResult") != "Ok":
            info = chk.get("ErrorInfo") or chk
            p["skip"] = ("the price is already above the buy-stop (the breakout came before the order)"
                         if info.get("ErrorCode") == "OnWrongSideOfMarket"
                         else "Saxo's precheck refused it: %s" % json.dumps(info)[:200])
            print("%s %s: skip, %s" % (name, a.date, p["skip"]))
            continue
        rate = chk.get("InstrumentToAccountConversionRate") or 1.0
        margin_usd = ((chk.get("MarginImpactBuySell") or {}).get("InitialMarginBuy") or 0.0) / rate
        p["margin_usd"] = round(margin_usd, 2)
        if used_usd + margin_usd > budget_usd:
            p["skip"] = "margin USD %.2f would pass the USD %.0f budget" % (margin_usd, budget_usd)
            print("%s %s: skip, %s" % (name, a.date, p["skip"]))
            continue
        if a.dry_run:
            print("%s %s: precheck Ok, margin USD %.2f; buy-stop %.2f, stop %.2f, target %.2f, %.1f contracts "
                  "(dry run, not sent)" % (name, a.date, margin_usd, p["entry"], p["stop"], p["target"], p["amount"]))
            continue
        res = c.send("POST", "/trade/v2/orders", body)
        p["order_id"] = res.get("OrderId")
        p["related_ids"] = [o.get("OrderId") for o in res.get("Orders", [])]
        used_usd += margin_usd
        print("%s %s: placed buy-stop %.2f (order %s), stop %.2f, target %.2f, %.1f contracts, USD %.2f at risk"
              % (name, a.date, p["entry"], p["order_id"], p["stop"], p["target"], p["amount"], p["risk_usd"]))
    if not a.dry_run:
        write_state(a.state, {"date": a.date, "rule": RULE, "risk_usd": a.risk, "capital_usd": a.capital,
                              "placed": True, "plans": plans})
    return 0


def cmd_flatten(a, c):
    acc = account_key(c)
    uics = {uic for uic, _ in INDICES.values()}
    orders = [o for o in _rows(c.get("/port/v1/orders/me")) if o.get("Uic") in uics and o.get("AssetType") == ASSET]
    if orders:
        ids = ",".join(str(o["OrderId"]) for o in orders)
        c.send("DELETE", "/trade/v2/orders/%s" % ids, params={"AccountKey": acc})
        print("%s: cancelled %d working order(s): %s" % (a.date, len(orders), ids))
    else:
        print("%s: no working orders to cancel" % a.date)
    closed = 0
    for row in _rows(c.get("/port/v1/netpositions/me", {"FieldGroups": "NetPositionBase"})):
        base = row.get("NetPositionBase") or {}
        if base.get("Uic") not in uics or base.get("AssetType") != ASSET or not base.get("Amount"):
            continue
        amt = float(base["Amount"])
        c.send("POST", "/trade/v2/orders", {"AccountKey": acc, "Uic": base["Uic"], "AssetType": ASSET,
                                            "BuySell": "Sell" if amt > 0 else "Buy", "Amount": abs(amt),
                                            "OrderType": "Market", "ManualOrder": False,
                                            "OrderDuration": {"DurationType": "DayOrder"}})
        closed += 1
        print("%s: closed %.1f contracts of uic %s at market" % (a.date, amt, base["Uic"]))
    if not closed:
        print("%s: no open position to close" % a.date)
    state = read_state(a.state)
    if state:
        state["flattened"] = True
        write_state(a.state, state)
    return 0


def closed_for_day(rows, day, uics):
    """Saxo closed positions -> those in the pilot's CFDs that closed on this New York date."""
    out = []
    for row in rows:
        cp = row.get("ClosedPosition") or row
        if cp.get("Uic") not in uics or cp.get("AssetType") != ASSET:
            continue
        t = cp.get("ExecutionTimeClose") or ""
        if not t:
            continue
        ny = to_eastern(datetime.datetime(1970, 1, 1) + datetime.timedelta(seconds=saxo._epoch(t)))
        if ny.date().isoformat() == day:
            out.append(cp)
    return out


def cmd_record(a, c):
    if journal is None:
        raise DemoError("record needs trader-journal next to trader-saxo")
    state = read_state(a.state)
    plans = {p.get("uic"): (name, p) for name, p in (state.get("plans") or {}).items()}
    uics = {uic for uic, _ in INDICES.values()}
    rows = closed_for_day(_rows(c.get("/port/v1/closedpositions/me", {"FieldGroups": "ClosedPosition"})),
                          a.date, uics)
    if not rows:
        print("%s: no closed demo position in the index CFDs. Nothing logged." % a.date)
        return 0
    doc = journal.load_journal(a.journal)
    trades = []
    for cp in rows:
        name, p = plans.get(cp.get("Uic"), (str(cp.get("Uic")), {}))
        if "stop" not in p:
            print("%s %s: closed position with no planned stop in %s; not logged." % (name, a.date, a.state))
            continue
        entry, exit_price = float(cp["OpenPrice"]), float(cp["ClosingPrice"])
        amount = abs(float(cp["Amount"]))
        if entry <= p["stop"]:
            print("%s %s: filled at %.2f, at or below the planned stop %.2f, so the trade has no R; not logged."
                  % (name, a.date, entry, p["stop"]))
            continue
        note = ("saxo-sim; planned entry %.2f, filled %.2f (%+.2f); stop %.2f, target %.2f; closed %.2f; "
                "P/L EUR %s" % (p["entry"], entry, entry - p["entry"], p["stop"], p["target"], exit_price,
                                cp.get("ProfitLossOnTradeInBaseCurrency")))
        trades.append(journal.make_trade(name, RULE["name"], "long", entry, p["stop"], exit_price, amount, a.date,
                                         costs=0, target=p.get("target"), point_value=1, asset_class="index_cfd",
                                         account="saxo-demo", note=note, source="demo.py"))
        print("%s %s: %s" % (name, a.date, note))
    added = journal.add_trades(doc, trades)
    journal.write_json(a.journal, doc)
    print("%s: logged %d new trade(s) to %s" % (a.date, len(added), a.journal))
    return 0


def main(argv=None, client=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("plan", "place", "flatten", "record"):
        s = sub.add_parser(name)
        s.add_argument("--date", required=True, help="the New York session date, YYYY-MM-DD")
        s.add_argument("--state", help="the day's state file (order ids and the plan)")
        s.add_argument("--risk", type=float, default=50.0, help="USD lost at the stop, per index (default 50)")
        s.add_argument("--capital", type=float, default=10000.0, help="the pilot's capital in USD (default 10000)")
        s.add_argument("--margin-share", type=float, default=0.5,
                       help="most of the capital the orders' initial margin may use (default 0.5)")
        s.add_argument("--journal", default="trader/saxo-demo-journal.json")
        s.add_argument("--dry-run", action="store_true", help="place: precheck only, send no order")
    a = ap.parse_args(argv)
    try:
        datetime.date.fromisoformat(a.date)
    except ValueError:
        ap.error("--date must be YYYY-MM-DD")
    try:
        c = client or Trader(pause=0.6)
        return {"plan": cmd_plan, "place": cmd_place, "flatten": cmd_flatten, "record": cmd_record}[a.cmd](a, c)
    except (DemoError, saxo.SaxoError) as e:
        print("demo: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
