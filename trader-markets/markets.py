#!/usr/bin/env python3
"""Instruments and trading sessions for trader-markets. Standard library only, no network.

One file, trader/instruments.json, says what each instrument is: its asset
class, the currency its price is quoted in, how much one unit moves per 1.0
of price (point_value), the smallest size and size step, the cost of a round
trip, and which trading session it follows. This tool reads it and answers:

  clock   is it open now? when does it close, or open next? in Singapore,
          New York and London time, for every instrument or the ones named.
  spec    one instrument's contract, as the file has it.
  risk    the money lost at the stop for a size, in the account currency.
  size    the biggest size whose loss at the stop fits a max loss, rounded
          DOWN to the instrument's size step.

    python3 markets.py clock
    python3 markets.py clock US500 XAUUSD EURUSD --now "2026-11-02 21:00" --tz sgt
    python3 markets.py risk --symbol USDJPY --entry 152.40 --stop 152.10 --qty 10000
    python3 markets.py size --symbol XAUUSD --entry 2400 --stop 2395 --max-loss 50

Sessions (all worked out in New York time, daylight saving included):

  us_stock    NYSE regular hours, 9:30 to 16:00, weekdays, closed on NYSE
              holidays, 13:00 close on early-close days (2026 and 2027 lists)
  fx_24x5     Sunday 17:00 to Friday 17:00, the forex week
  cme_globex  Sunday 18:00 to Friday 17:00 with a 17:00 to 18:00 break each
              day: CME's hours for its gold and equity-index futures, the
              default for gold and index CFDs. A broker's CFD hours can
              differ, and holiday hours are not modelled: check the broker.

Money in another currency (USDJPY's profit is in yen) is converted to the
account currency. For a pair quoted as ACCOUNT/QUOTE (USDJPY in a USD
account) the rate is 1 / price at the stop; any other pair needs --fx.
Nothing here is guessed: every contract number comes from the file.
"""

import argparse
import datetime
import json
import math
import os
import sys

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
NYSE_EARLY_CLOSE = {  # 1:00 p.m. ET
    "2026-11-27": "day after Thanksgiving",
    "2026-12-24": "Christmas Eve",
    "2027-11-26": "day after Thanksgiving",
}
CALENDAR_YEARS = (2026, 2027)
SESSIONS = ("us_stock", "fx_24x5", "cme_globex")
ZONES = ("sgt", "ny", "london")
HERE = os.path.dirname(os.path.abspath(__file__))
EXAMPLE = os.path.join(HERE, "instruments.example.json")
EPS = 1e-9


class MarketError(Exception):
    pass


# ---------------------------------------------------------------- time (no tz files)

def _nth_sunday(year, month, n):
    d = datetime.date(year, month, 1)
    d += datetime.timedelta(days=(6 - d.weekday()) % 7)
    return d + datetime.timedelta(weeks=n - 1)


def _last_sunday(year, month):
    d = datetime.date(year, month + 1, 1) - datetime.timedelta(days=1)       # March or October
    return d - datetime.timedelta(days=(d.weekday() + 1) % 7)


def eastern_offset(utc):
    """Hours from UTC for New York: -4 from 2:00 a.m. on the second Sunday of March
    to 2:00 a.m. on the first Sunday of November (US rule since 2007), else -5."""
    y = utc.year
    start = datetime.datetime.combine(_nth_sunday(y, 3, 2), datetime.time(7, 0))
    end = datetime.datetime.combine(_nth_sunday(y, 11, 1), datetime.time(6, 0))
    return -4 if start <= utc < end else -5


def london_offset(utc):
    """Hours from UTC for London: +1 from 01:00 UTC on the last Sunday of March to
    01:00 UTC on the last Sunday of October (UK and EU rule), else 0."""
    y = utc.year
    start = datetime.datetime.combine(_last_sunday(y, 3), datetime.time(1, 0))
    end = datetime.datetime.combine(_last_sunday(y, 10), datetime.time(1, 0))
    return 1 if start <= utc < end else 0


def local(utc, zone):
    hours = {"sgt": 8, "ny": eastern_offset(utc), "london": london_offset(utc)}[zone]
    return utc + datetime.timedelta(hours=hours)


def ny_to_utc(ny):
    """New York wall time -> naive UTC (the offset is the one in force at that wall time)."""
    guess = ny + datetime.timedelta(hours=5)
    return ny - datetime.timedelta(hours=eastern_offset(guess))


def parse_now(text, tz="ny"):
    """None -> the real clock, as naive UTC. Else "YYYY-MM-DD HH:MM" in --tz."""
    if not text:
        return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None, second=0, microsecond=0)
    try:
        wall = datetime.datetime.strptime(text.strip(), "%Y-%m-%d %H:%M")
    except ValueError:
        raise MarketError('--now wants "YYYY-MM-DD HH:MM", like "2026-11-02 21:00"')
    if tz == "ny":
        return ny_to_utc(wall)
    if tz == "sgt":
        return wall - datetime.timedelta(hours=8)
    guess = wall - datetime.timedelta(hours=1)
    return wall - datetime.timedelta(hours=london_offset(guess))


# ---------------------------------------------------------------- sessions

def _minutes(t):
    return t.hour * 60 + t.minute


def is_open(session, ny):
    """Open at this New York wall-clock minute?"""
    wd, m = ny.weekday(), _minutes(ny)            # Monday 0 .. Sunday 6
    if session == "us_stock":
        d = ny.date().isoformat()
        if wd >= 5 or d in NYSE_HOLIDAYS:
            return False
        close = 13 * 60 if d in NYSE_EARLY_CLOSE else 16 * 60
        return 9 * 60 + 30 <= m < close
    if session == "fx_24x5":
        if wd == 5:
            return False
        if wd == 6:
            return m >= 17 * 60
        if wd == 4:
            return m < 17 * 60
        return True
    if session == "cme_globex":
        if wd == 5:
            return False
        if wd == 6:
            return m >= 18 * 60
        if wd == 4:
            return m < 17 * 60
        return not 17 * 60 <= m < 18 * 60
    raise MarketError("unknown session %r: use one of %s" % (session, ", ".join(SESSIONS)))


def next_change(session, utc, limit_days=10):
    """(open now?, UTC minute of the next close or open), scanning minute by minute."""
    now_open = is_open(session, local(utc, "ny"))
    t = utc
    end = utc + datetime.timedelta(days=limit_days)
    while t < end:
        t += datetime.timedelta(minutes=1)
        if is_open(session, local(t, "ny")) != now_open:
            return now_open, t
    return now_open, None


def calendar_note(session, utc):
    ny = local(utc, "ny")
    if session == "us_stock" and ny.year not in CALENDAR_YEARS:
        return "the NYSE holiday list covers %s only: check nyse.com" % " and ".join(map(str, CALENDAR_YEARS))
    if session != "us_stock":
        return "holiday hours are not modelled: check the broker"
    return None


# ---------------------------------------------------------------- instruments

def _num(spec, key, sym, default=None, positive=True):
    v = spec.get(key, default)
    try:
        v = float(v)
    except (TypeError, ValueError):
        raise MarketError("%s: %s must be a number, not %r" % (sym, key, spec.get(key)))
    if not math.isfinite(v) or (v <= 0 if positive else v < 0):
        raise MarketError("%s: %s must be %s" % (sym, key, "positive" if positive else "zero or more"))
    return v


def load(path=None):
    """The instruments file (else this folder's example), checked. Returns (account currency, {SYM: spec})."""
    use = path if path and os.path.exists(path) else EXAMPLE
    try:
        with open(use, encoding="utf-8") as fh:
            doc = json.load(fh)
    except json.JSONDecodeError as e:
        raise MarketError("%s is not valid JSON: %s" % (use, e))
    acct = str(doc.get("account_currency") or "USD").upper()
    out = {}
    for sym, raw in (doc.get("instruments") or {}).items():
        sym = sym.strip().upper()
        if not isinstance(raw, dict):
            raise MarketError("%s: the spec must be an object" % sym)
        session = raw.get("session")
        if session not in SESSIONS:
            raise MarketError("%s: session must be one of %s, not %r" % (sym, ", ".join(SESSIONS), session))
        spec = {
            "symbol": sym, "asset_class": raw.get("asset_class") or "us_stock",
            "quote_currency": str(raw.get("quote_currency") or "USD").upper(), "session": session,
            "point_value": _num(raw, "point_value", sym, 1),
            "min_qty": _num(raw, "min_qty", sym, 1),
            "qty_step": _num(raw, "qty_step", sym, 1),
            "cost_per_trade": _num(raw, "cost_per_trade", sym, 0, positive=False),
        }
        for k in ("description", "_check"):
            if raw.get(k):
                spec[k] = raw[k]
        out[sym] = spec
    return acct, out, use


def get(instruments, sym):
    sym = str(sym or "").strip().upper()
    if sym not in instruments:
        raise MarketError("%s is not in the instruments file; add it with its broker contract spec" % sym)
    return instruments[sym]


def fx_rate(spec, account, price, fx=None):
    """Account money per 1 unit of the quote currency."""
    q = spec["quote_currency"]
    if q == account:
        return 1.0
    if fx is not None:
        if fx <= 0:
            raise MarketError("--fx must be positive")
        return fx
    if spec["symbol"] == account + q:
        return 1.0 / price
    raise MarketError("%s is quoted in %s and the account is in %s: pass --fx, the %s value of 1 %s"
                      % (spec["symbol"], q, account, account, q))


def loss_at_stop(spec, account, entry, stop, qty, fx=None):
    """(loss in account money including the round-trip cost, rate used). Long or short by the stop's side."""
    if entry <= 0 or stop <= 0 or qty <= 0 or abs(entry - stop) < EPS:
        raise MarketError("entry, stop and qty must be positive and the stop must differ from the entry")
    rate = fx_rate(spec, account, stop, fx)
    move = abs(entry - stop) * spec["point_value"] * qty * rate
    return round(move + spec["cost_per_trade"] * qty, 2), rate


def size_for(spec, account, entry, stop, max_loss, fx=None):
    """Biggest size, rounded down to qty_step, whose loss at the stop fits max_loss. 0 if even min_qty does not."""
    if max_loss <= 0:
        raise MarketError("--max-loss must be positive")
    rate = loss_at_stop(spec, account, entry, stop, 1.0, fx)[1]      # also checks entry and stop
    per_unit = abs(entry - stop) * spec["point_value"] * rate + spec["cost_per_trade"]
    step = spec["qty_step"]
    qty = math.floor(max_loss / per_unit / step + EPS) * step
    qty = float("%.10g" % qty)
    if qty < spec["min_qty"] - EPS:
        return 0.0, per_unit, rate
    return qty, per_unit, rate


# ---------------------------------------------------------------- printing

def fmt(utc, zone):
    t = local(utc, zone)
    return t.strftime("%a %d %b %H:%M")


def clock_rows(instruments, syms, utc):
    rows = []
    for sym in syms or sorted(instruments):
        spec = get(instruments, sym)
        is_on, change = next_change(spec["session"], utc)
        rows.append({"symbol": spec["symbol"], "session": spec["session"], "open": is_on,
                     "next": change.strftime("%Y-%m-%dT%H:%MZ") if change else None,
                     "next_is": "close" if is_on else "open",
                     "next_local": {z: fmt(change, z) for z in ZONES} if change else None,
                     "note": calendar_note(spec["session"], utc)})
    return rows


def clock_report(rows, utc, tz):
    lines = ["Now: %s SGT | %s New York | %s London" % (fmt(utc, "sgt"), fmt(utc, "ny"), fmt(utc, "london")), ""]
    for r in rows:
        when = r["next_local"][tz] if r["next_local"] else "not within 10 days"
        lines.append("%-7s %-10s %-6s %s %s %s" % (r["symbol"], r["session"], "OPEN" if r["open"] else "closed",
                                                   r["next_is"] + "s", when, tz.upper()))
    notes = sorted({r["note"] for r in rows if r["note"]})
    for n in notes:
        lines.append("Note: " + n + ".")
    return "\n".join(lines)


# ---------------------------------------------------------------- commands

def main(argv=None):
    ap = argparse.ArgumentParser(description="Instruments and trading sessions: open or closed, risk and size.")
    ap.add_argument("--instruments", default="trader/instruments.json")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("clock", help="open or closed now, and the next change")
    p.add_argument("symbols", nargs="*")
    p.add_argument("--now", help='pin the clock, "YYYY-MM-DD HH:MM" in --tz')
    p.add_argument("--tz", default="sgt", choices=ZONES, help="the zone to show and to read --now in")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("spec", help="one instrument's contract")
    p.add_argument("symbol")
    for name in ("risk", "size"):
        p = sub.add_parser(name, help="loss at the stop for a size" if name == "risk" else "size for a max loss")
        p.add_argument("--symbol", required=True)
        p.add_argument("--entry", type=float, required=True)
        p.add_argument("--stop", type=float, required=True)
        if name == "risk":
            p.add_argument("--qty", type=float, required=True)
        else:
            p.add_argument("--max-loss", type=float, required=True)
        p.add_argument("--fx", type=float, help="account money per 1 unit of the quote currency")
        p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        acct, inst, used = load(a.instruments)
        if a.cmd == "clock":
            utc = parse_now(a.now, a.tz)
            rows = clock_rows(inst, a.symbols, utc)
            print(json.dumps(rows, indent=2) if a.json else clock_report(rows, utc, a.tz))
            if used == EXAMPLE and not a.json:
                print("Using the example instruments (no %s): its contract numbers are placeholders." % a.instruments)
        elif a.cmd == "spec":
            print(json.dumps(dict(get(inst, a.symbol), account_currency=acct), indent=2))
        elif a.cmd == "risk":
            spec = get(inst, a.symbol)
            loss, rate = loss_at_stop(spec, acct, a.entry, a.stop, a.qty, a.fx)
            out = {"symbol": spec["symbol"], "qty": a.qty, "loss_at_stop": loss, "account_currency": acct,
                   "fx": rate}
            print(json.dumps(out, indent=2) if a.json else
                  "%s %g from %g, stop %g: %s %.2f at the stop including costs%s."
                  % (spec["symbol"], a.qty, a.entry, a.stop, acct, loss,
                     "" if rate == 1 else " (1 %s = %.6g %s)" % (spec["quote_currency"], rate, acct)))
        else:
            spec = get(inst, a.symbol)
            qty, per, rate = size_for(spec, acct, a.entry, a.stop, a.max_loss, a.fx)
            loss = loss_at_stop(spec, acct, a.entry, a.stop, qty, a.fx)[0] if qty else 0.0
            out = {"symbol": spec["symbol"], "qty": qty, "loss_at_stop": loss, "per_unit": round(per, 6),
                   "account_currency": acct, "fx": rate}
            if a.json:
                print(json.dumps(out, indent=2))
            elif qty:
                print("%s: %g (step %g), %s %.2f at the stop including costs, inside your %s %.2f."
                      % (spec["symbol"], qty, spec["qty_step"], acct, loss, acct, a.max_loss))
            else:
                print("%s: no size fits. The smallest size, %g, loses %s %.2f at that stop, more than %s %.2f."
                      % (spec["symbol"], spec["min_qty"], acct, per * spec["min_qty"], acct, a.max_loss))
        return 0
    except MarketError as e:
        print("markets: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
