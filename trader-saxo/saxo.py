#!/usr/bin/env python3
"""Read-only Saxo OpenAPI client for trader-saxo. Standard library only.

It reads your Saxo simulation (demo) account and Saxo's instrument data. It
cannot place, change or cancel an order: there is no order endpoint in this
file and every request is a GET.

  whoami     the user and accounts the token belongs to
  balance    cash, total value, margin available, open positions
  positions  open positions with P&L, in plain rows
  find       search instruments by keyword -> Uic and AssetType
  details    one instrument's trading rules (minimum size, step, currency, ...)
  link       record a symbol's Uic and AssetType in trader/instruments.json,
             and fill min_qty, qty_step and quote_currency from Saxo's details
  quote      prices for linked symbols -> trader/prices.json (the file the
             risk check and sizing read), with Saxo's delay and price type
  bars       chart bars for a linked symbol, saved as a Yahoo-style chart
             JSON so trader-timemachine and trader-entry can read it offline

    export SAXO_TOKEN=...            # the 24-hour token from developer.saxo, never in the chat
    python3 saxo.py whoami
    python3 saxo.py find "S&P 500" --asset-types CfdOnIndex
    python3 saxo.py link US500 --uic <Uic from find> --asset-type CfdOnIndex --write
    python3 saxo.py quote US500 NVDA --write
    python3 saxo.py bars US500 --horizon 5 --count 1200 --out trader/charts/US500-5m.json

The base URL is Saxo's simulation gateway. There is no live switch in this
version on purpose: live access comes after the demo has shown the setup
works, and with its own review.
"""

import argparse
import datetime
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

SIM = "https://gateway.saxobank.com/sim/openapi"
TOKEN_ENV = "SAXO_TOKEN"
UA = "day-trader-skill/2.0 (trader-saxo, read-only)"
CHART_HORIZONS = (1, 2, 3, 5, 10, 15, 30, 60, 120, 180, 240, 300, 360, 480, 1440, 10080, 43200, 129600, 518400)
STALE_TYPES = ("NoAccess", "NoMarket", "OldIndicative", "Pending")


class SaxoError(Exception):
    pass


# ---------------------------------------------------------------- transport (GET only)

class Client:
    def __init__(self, token=None, base=SIM, opener=None, pause=0.5):
        self.token = token if token is not None else os.environ.get(TOKEN_ENV, "").strip()
        if not self.token:
            raise SaxoError("no token: set %s in the environment (the 24-hour token from "
                            "https://www.developer.saxo/openapi/token). Never paste it into a chat." % TOKEN_ENV)
        self.base = base.rstrip("/")
        self.opener = opener or urllib.request.urlopen
        self.pause = pause

    def get(self, path, params=None):
        q = {k: v for k, v in (params or {}).items() if v is not None}
        url = self.base + path + ("?" + urllib.parse.urlencode(q, safe=",$") if q else "")
        req = urllib.request.Request(url, method="GET", headers={
            "Authorization": "Bearer " + self.token, "Accept": "application/json", "User-Agent": UA})
        for attempt in (1, 2):
            try:
                with self.opener(req, timeout=20) as r:
                    body = r.read().decode("utf-8")
                    return json.loads(body) if body else {}
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt == 1:
                    time.sleep(max(self.pause, 2.0))
                    continue
                if e.code == 401:
                    raise SaxoError("Saxo said 401 unauthorised: the token is missing, wrong or past its "
                                    "24 hours. Get a new one at https://www.developer.saxo/openapi/token")
                detail = ""
                try:
                    detail = e.read().decode("utf-8")[:300]
                except Exception:
                    pass
                raise SaxoError("Saxo returned HTTP %d for %s %s" % (e.code, path, detail))
            except urllib.error.URLError as e:
                raise SaxoError("could not reach Saxo: %s" % e.reason)
            except ValueError:
                raise SaxoError("Saxo returned something that is not JSON for %s" % path)
        raise SaxoError("Saxo kept rate-limiting %s (HTTP 429)" % path)


# ---------------------------------------------------------------- reads

def whoami(c):
    user = c.get("/port/v1/users/me")
    accounts = c.get("/port/v1/accounts/me").get("Data", [])
    return {"name": user.get("Name"), "client_key": user.get("ClientKey"),
            "legal_asset_types": user.get("LegalAssetTypes"),
            "market_data_terms_accepted": user.get("MarketDataViaOpenApiTermsAccepted"),
            "accounts": [{k: a.get(k) for k in ("AccountId", "AccountKey", "Currency", "AccountType", "Active",
                                                 "IsTrialAccount")} for a in accounts]}


BALANCE_FIELDS = ("Currency", "CashBalance", "CashAvailableForTrading", "TotalValue", "MarginAvailableForTrading",
                  "MarginUtilizationPct", "UnrealizedMarginProfitLoss", "NetPositionsCount", "OpenPositionsCount",
                  "OrdersCount")


def balance(c):
    b = c.get("/port/v1/balances/me")
    return {k: b.get(k) for k in BALANCE_FIELDS}


def positions(c):
    data = c.get("/port/v1/positions/me", {"FieldGroups": "PositionBase,PositionView,DisplayAndFormat"})
    rows = []
    for p in data.get("Data", []):
        base, view, disp = p.get("PositionBase") or {}, p.get("PositionView") or {}, p.get("DisplayAndFormat") or {}
        rows.append({"symbol": disp.get("Symbol"), "description": disp.get("Description"),
                     "uic": base.get("Uic"), "asset_type": base.get("AssetType"), "amount": base.get("Amount"),
                     "open_price": base.get("OpenPrice"), "current_price": view.get("CurrentPrice"),
                     "delay_minutes": view.get("CurrentPriceDelayMinutes"),
                     "pnl": view.get("ProfitLossOnTrade"), "pnl_base": view.get("ProfitLossOnTradeInBaseCurrency"),
                     "currency": disp.get("Currency"), "status": base.get("Status")})
    return rows


def find(c, keywords, asset_types=None, top=20):
    data = c.get("/ref/v1/instruments", {"Keywords": keywords, "AssetTypes": asset_types, "$top": top})
    return [{"uic": d.get("Identifier"), "asset_type": d.get("AssetType"), "symbol": d.get("Symbol"),
             "description": d.get("Description"), "exchange": d.get("ExchangeId")}
            for d in data.get("Data", [])]


DETAIL_FIELDS = ("Uic", "AssetType", "Symbol", "Description", "CurrencyCode", "PriceCurrency", "MinimumTradeSize",
                 "IncrementSize", "LotSize", "MinimumLotSize", "AmountDecimals", "ContractSize", "PriceToContractFactor",
                 "TickSize", "MinimumOrderValue", "IsTradable", "TradingStatus", "NonTradableReason",
                 "SupportedOrderTypes", "Format")


def details(c, uic, asset_type):
    d = c.get("/ref/v1/instruments/details/%s/%s" % (int(uic), asset_type))
    return {k: d.get(k) for k in DETAIL_FIELDS if k in d}


# ---------------------------------------------------------------- instruments file

def read_json(path, default=None):
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except ValueError as e:
        raise SaxoError("%s is not valid JSON: %s" % (path, e))


def write_json(path, data):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def link_spec(spec, det):
    """Merge Saxo's details into one instruments.json entry. Only what Saxo states
    directly is copied; point_value and cost_per_trade are never filled in here."""
    out = dict(spec or {})
    out["saxo"] = {"uic": det.get("Uic"), "asset_type": det.get("AssetType"), "symbol": det.get("Symbol"),
                   "description": det.get("Description")}
    changed = []
    if det.get("CurrencyCode"):
        out["quote_currency"] = det["CurrencyCode"]
        changed.append("quote_currency")
    if det.get("MinimumTradeSize") is not None:
        out["min_qty"] = det["MinimumTradeSize"]
        changed.append("min_qty")
    if det.get("IncrementSize") is not None:
        out["qty_step"] = det["IncrementSize"]
        changed.append("qty_step")
    unconfirmed = [k for k in ("point_value", "cost_per_trade", "session") if k not in out]
    out["_saxo_details"] = {k: det.get(k) for k in ("ContractSize", "PriceToContractFactor", "LotSize", "TickSize",
                                                      "AmountDecimals", "MinimumOrderValue") if k in det}
    out["_check"] = ("min_qty, qty_step and quote_currency from Saxo's details; point_value, cost_per_trade and "
                     "session are yours to confirm (see _saxo_details)")
    return out, changed, unconfirmed


def linked(inst_doc, sym):
    spec = ((inst_doc or {}).get("instruments") or {}).get(sym.upper()) or {}
    sx = spec.get("saxo") or {}
    if not sx.get("uic") or not sx.get("asset_type"):
        raise SaxoError("%s has no Saxo link in the instruments file: run saxo.py find, then saxo.py link %s "
                        "--uic ... --asset-type ... --write" % (sym.upper(), sym.upper()))
    return int(sx["uic"]), sx["asset_type"]


# ---------------------------------------------------------------- prices

def quote(c, uic, asset_type):
    d = c.get("/trade/v1/infoprices", {"Uic": uic, "AssetType": asset_type, "FieldGroups": "Quote,PriceInfo"})
    q = d.get("Quote") or {}
    return {"bid": q.get("Bid"), "ask": q.get("Ask"), "mid": q.get("Mid"), "delay_minutes": q.get("DelayedByMinutes"),
            "price_type_bid": q.get("PriceTypeBid"), "price_type_ask": q.get("PriceTypeAsk"),
            "time": d.get("LastUpdated"), "error": d.get("ErrorCode") or q.get("ErrorCode")}


def usable_price(q):
    """(price, reason it is not usable). A long's risk is measured from the ask; no ask, no price."""
    if q.get("error"):
        return None, "Saxo error %s" % q["error"]
    for t in (q.get("price_type_ask"), q.get("price_type_bid")):
        if t in STALE_TYPES:
            return None, "price type %s" % t
    if q.get("ask") is None:
        return None, "no ask"
    return float(q["ask"]), None


def write_prices(path, quotes, now_utc):
    old = read_json(path, {}) or {}
    prices, detail = dict(old.get("prices", {})), dict(old.get("detail", {}))
    stamp = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    for s, (price, q) in quotes.items():
        prices[s] = price
        detail[s] = {"time": q.get("time"), "source": "saxo-sim", "fetched": stamp, "bid": q.get("bid"),
                     "ask": q.get("ask"), "delay_minutes": q.get("delay_minutes")}
    doc = {"as_of": stamp, "source": "saxo-sim",
           "note": "Saxo simulation prices (the ask). delay_minutes > 0 means delayed. The fill is the broker's.",
           "prices": prices, "detail": detail}
    write_json(path, doc)
    return doc


# ---------------------------------------------------------------- bars

def _epoch(iso):
    t = datetime.datetime.strptime(iso.rstrip("Z").split(".")[0], "%Y-%m-%dT%H:%M:%S")
    return int((t - datetime.datetime(1970, 1, 1)).total_seconds())


def _ohlc(row):
    """Last-traded OHLC, else the bid/ask midpoint (Saxo returns bid and ask bars for FX)."""
    if row.get("Close") is not None:
        return [row.get(k) for k in ("Open", "High", "Low", "Close")]
    out = []
    for k in ("Open", "High", "Low", "Close"):
        b, a = row.get(k + "Bid"), row.get(k + "Ask")
        out.append(None if b is None or a is None else (b + a) / 2.0)
    return out


def bars(c, uic, asset_type, horizon=5, count=1200):
    if horizon not in CHART_HORIZONS:
        raise SaxoError("--horizon must be one of %s minutes" % ", ".join(map(str, CHART_HORIZONS)))
    if not 1 <= count <= 1200:
        raise SaxoError("--count must be 1 to 1200 (Saxo's limit)")
    return c.get("/chart/v3/charts", {"Uic": uic, "AssetType": asset_type, "Horizon": horizon, "Count": count})


def to_yahoo(sym, chart, horizon):
    """Saxo chart v3 -> a Yahoo v8 chart JSON, so the pack's offline readers take it."""
    ts, o, h, l, cl, v = [], [], [], [], [], []
    for row in chart.get("Data", []):
        if not row.get("Time"):
            continue
        p = _ohlc(row)
        ts.append(_epoch(row["Time"]))
        for col, x in zip((o, h, l, cl), p):
            col.append(x)
        v.append(row.get("Volume") or 0)
    info = chart.get("ChartInfo") or {}
    meta = {"symbol": sym.upper(), "dataGranularity": "%dm" % horizon, "source": "saxo-sim",
            "delayedByMinutes": info.get("DelayedByMinutes"), "regularMarketTime": ts[-1] if ts else None}
    return {"chart": {"result": [{"meta": meta, "timestamp": ts,
                                  "indicators": {"quote": [{"open": o, "high": h, "low": l, "close": cl,
                                                            "volume": v}]}}], "error": None}}


# ---------------------------------------------------------------- printing

def table(rows, cols):
    if not rows:
        return "(none)"
    w = {k: max(len(k), *(len("" if r.get(k) is None else str(r.get(k))) for r in rows)) for k in cols}
    out = ["  ".join(k.ljust(w[k]) for k in cols)]
    for r in rows:
        out.append("  ".join(("" if r.get(k) is None else str(r.get(k))).ljust(w[k]) for k in cols))
    return "\n".join(out)


# ---------------------------------------------------------------- commands

def main(argv=None, client=None, now=None):
    ap = argparse.ArgumentParser(description="Read-only Saxo OpenAPI (simulation): account, instruments, prices.")
    ap.add_argument("--instruments", default="trader/instruments.json")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("whoami")
    sub.add_parser("balance")
    sub.add_parser("positions")
    p = sub.add_parser("find")
    p.add_argument("keywords")
    p.add_argument("--asset-types", help="comma-separated, e.g. Stock,CfdOnIndex,FxSpot")
    p.add_argument("--top", type=int, default=20)
    p = sub.add_parser("details")
    p.add_argument("uic", type=int)
    p.add_argument("asset_type")
    p = sub.add_parser("link")
    p.add_argument("symbol")
    p.add_argument("--uic", type=int, required=True)
    p.add_argument("--asset-type", required=True)
    p.add_argument("--write", action="store_true")
    p = sub.add_parser("quote")
    p.add_argument("symbols", nargs="+")
    p.add_argument("--write", nargs="?", const="trader/prices.json", metavar="PATH")
    p = sub.add_parser("bars")
    p.add_argument("symbol")
    p.add_argument("--horizon", type=int, default=5)
    p.add_argument("--count", type=int, default=1200)
    p.add_argument("--out", help="save as a Yahoo-style chart JSON")
    for q in sub.choices.values():
        q.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        c = client or Client()
        now = now or datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None, microsecond=0)
        if a.cmd == "whoami":
            res = whoami(c)
            text = "%s, accounts:\n%s" % (res["name"], table(res["accounts"], ("AccountId", "Currency", "AccountType",
                                                                               "Active", "IsTrialAccount")))
        elif a.cmd == "balance":
            res = balance(c)
            text = "\n".join("%-27s %s" % (k, res[k]) for k in BALANCE_FIELDS)
        elif a.cmd == "positions":
            res = positions(c)
            text = table(res, ("symbol", "amount", "open_price", "current_price", "pnl", "currency", "delay_minutes"))
        elif a.cmd == "find":
            res = find(c, a.keywords, a.asset_types, a.top)
            text = table(res, ("uic", "asset_type", "symbol", "description", "exchange"))
        elif a.cmd == "details":
            res = details(c, a.uic, a.asset_type)
            text = "\n".join("%-22s %s" % (k, v) for k, v in res.items())
        elif a.cmd == "link":
            det = details(c, a.uic, a.asset_type)
            doc = read_json(a.instruments, {"account_currency": "USD", "instruments": {}})
            insts = doc.setdefault("instruments", {})
            spec, changed, unconfirmed = link_spec(insts.get(a.symbol.upper()), det)
            res = {"symbol": a.symbol.upper(), "spec": spec, "from_saxo": changed, "still_to_confirm": unconfirmed}
            text = ("%s -> Saxo %s %s (%s). From Saxo: %s.%s"
                    % (a.symbol.upper(), det.get("Uic"), det.get("AssetType"), det.get("Description"),
                       ", ".join(changed) or "nothing",
                       (" Still yours to set: %s." % ", ".join(unconfirmed)) if unconfirmed else ""))
            if a.write:
                insts[a.symbol.upper()] = spec
                write_json(a.instruments, doc)
                text += " Wrote %s." % a.instruments
        elif a.cmd == "quote":
            doc = read_json(a.instruments, {}) or {}
            got, res = {}, []
            for s in a.symbols:
                uic, at = linked(doc, s)
                q = quote(c, uic, at)
                price, why = usable_price(q)
                res.append(dict(q, symbol=s.upper(), used=price, not_used=why))
                if price is not None:
                    got[s.upper()] = (price, q)
            text = table(res, ("symbol", "bid", "ask", "delay_minutes", "price_type_ask", "used", "not_used"))
            if a.write and got:
                write_prices(a.write, got, now)
                text += "\nWrote %d price(s) to %s." % (len(got), a.write)
        else:
            doc = read_json(a.instruments, {}) or {}
            uic, at = linked(doc, a.symbol)
            chart = bars(c, uic, at, a.horizon, a.count)
            res = to_yahoo(a.symbol, chart, a.horizon)
            r0 = res["chart"]["result"][0]
            text = "%s: %d bars of %d minutes, delayed %s minutes." % (
                a.symbol.upper(), len(r0["timestamp"]), a.horizon, r0["meta"]["delayedByMinutes"])
            if a.out:
                write_json(a.out, res)
                text += " Wrote %s." % a.out
        print(json.dumps(res, indent=2) if a.json else text)
        return 0
    except SaxoError as e:
        print("saxo: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
