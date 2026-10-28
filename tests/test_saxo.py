"""trader-saxo against a fake gateway. The responses below are SYNTHETIC, shaped
from the field names in Saxo's OpenAPI reference docs; no number in them is
real Saxo data."""

import datetime
import io
import json
import os
import re
import shutil
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT

sx = load("trader-saxo", "saxo")
mk = load("trader-markets", "markets")

NOW = datetime.datetime(2026, 10, 28, 14, 0, 0)
FAKE = {
    "/port/v1/users/me": {"Name": "Demo User", "ClientKey": "ck", "LegalAssetTypes": ["Stock", "FxSpot"],
                          "MarketDataViaOpenApiTermsAccepted": True},
    "/port/v1/accounts/me": {"Data": [{"AccountId": "DEMO-1", "AccountKey": "ak", "Currency": "USD",
                                       "AccountType": "Normal", "Active": True, "IsTrialAccount": True}]},
    "/port/v1/balances/me": {"Currency": "USD", "CashBalance": 100000.0, "TotalValue": 100000.0,
                             "MarginAvailableForTrading": 100000.0, "NetPositionsCount": 0},
    "/port/v1/positions/me": {"Data": [{"PositionBase": {"Uic": 111, "AssetType": "Stock", "Amount": 8,
                                                         "OpenPrice": 364.2, "Status": "Open"},
                                        "PositionView": {"CurrentPrice": 366.0, "ProfitLossOnTrade": 14.4,
                                                         "CurrentPriceDelayMinutes": 15},
                                        "DisplayAndFormat": {"Symbol": "TSLA:xnas", "Currency": "USD"}}]},
    "/ref/v1/instruments": {"Data": [{"Identifier": 222, "AssetType": "CfdOnIndex", "Symbol": "US500.I",
                                      "Description": "US 500 (synthetic)", "ExchangeId": "CBOT"}]},
    "/ref/v1/instruments/details/222/CfdOnIndex": {"Uic": 222, "AssetType": "CfdOnIndex", "Symbol": "US500.I",
                                                   "Description": "US 500 (synthetic)", "CurrencyCode": "USD",
                                                   "MinimumTradeSize": 0.1, "IncrementSize": 0.1,
                                                   "ContractSize": 1, "TickSize": 0.25},
    "/trade/v1/infoprices": {"LastUpdated": "2026-10-28T13:59:00Z",
                             "Quote": {"Bid": 6800.0, "Ask": 6800.5, "Mid": 6800.25, "DelayedByMinutes": 15,
                                       "PriceTypeBid": "Indicative", "PriceTypeAsk": "Indicative"}},
    "/chart/v3/charts": {"ChartInfo": {"DelayedByMinutes": 15, "Horizon": 5},
                         "Data": [{"Time": "2026-10-27T13:30:00.000000Z", "Open": 10, "High": 12, "Low": 9,
                                   "Close": 11, "Volume": 5},
                                  {"Time": "2026-10-27T13:35:00Z", "OpenBid": 1.0, "OpenAsk": 1.2, "HighBid": 2.0,
                                   "HighAsk": 2.2, "LowBid": 0.5, "LowAsk": 0.7, "CloseBid": 1.5,
                                   "CloseAsk": 1.7}]},
}


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Gateway:
    """Records every request; answers from FAKE by path."""

    def __init__(self, fake=FAKE, status=None):
        self.fake, self.status, self.calls = fake, status, []

    def __call__(self, req, timeout=None):
        self.calls.append(req)
        if self.status:
            raise urllib.error.HTTPError(req.full_url, self.status, "x", {}, io.BytesIO(b"{}"))
        path = req.full_url.split("/sim/openapi", 1)[1].split("?")[0]
        return Resp(json.dumps(self.fake[path]).encode())


def client(**kw):
    gw = Gateway(**kw)
    return sx.Client(token="t", opener=gw, pause=0), gw


class ReadOnly(unittest.TestCase):
    def test_the_file_has_no_way_to_place_an_order(self):
        with open(os.path.join(ROOT, "trader-saxo", "saxo.py")) as fh:
            src = fh.read()
        for banned in ("/trade/v2/orders", "/trade/v1/orders", "POST", "PUT", "PATCH", "DELETE"):
            self.assertNotIn(banned, src.replace('method="GET"', ""), banned)

    def test_every_request_is_a_get_to_the_sim_gateway_with_the_token(self):
        c, gw = client()
        sx.whoami(c)
        sx.balance(c)
        for r in gw.calls:
            self.assertEqual(r.get_method(), "GET")
            self.assertTrue(r.full_url.startswith("https://gateway.saxobank.com/sim/openapi/"))
            self.assertEqual(r.get_header("Authorization"), "Bearer t")

    def test_no_token_is_a_plain_refusal(self):
        old = os.environ.pop("SAXO_TOKEN", None)
        self.addCleanup(lambda: old is not None and os.environ.__setitem__("SAXO_TOKEN", old))
        with self.assertRaises(sx.SaxoError) as e:
            sx.Client()
        self.assertIn("Never paste it", str(e.exception))

    def test_an_expired_token_says_so(self):
        c, _ = client(status=401)
        with self.assertRaises(sx.SaxoError) as e:
            sx.balance(c)
        self.assertIn("24 hours", str(e.exception))


class Reads(unittest.TestCase):
    def test_account_balance_positions_find_details(self):
        c, gw = client()
        self.assertEqual(sx.whoami(c)["accounts"][0]["Currency"], "USD")
        self.assertEqual(sx.balance(c)["CashBalance"], 100000.0)
        pos = sx.positions(c)[0]
        self.assertEqual((pos["amount"], pos["pnl"], pos["delay_minutes"]), (8, 14.4, 15))
        self.assertIn("FieldGroups=PositionBase,PositionView,DisplayAndFormat", gw.calls[-1].full_url)
        self.assertEqual(sx.find(c, "US 500", "CfdOnIndex")[0]["uic"], 222)
        self.assertIn("AssetTypes=CfdOnIndex", gw.calls[-1].full_url)
        self.assertEqual(sx.details(c, 222, "CfdOnIndex")["MinimumTradeSize"], 0.1)


class Link(unittest.TestCase):
    def test_link_copies_only_what_saxo_states(self):
        example = mk.load(os.path.join(ROOT, "trader-markets", "instruments.example.json"))[1]["US500"]
        raw = {k: example[k] for k in ("asset_class", "quote_currency", "session", "point_value", "min_qty",
                                        "qty_step", "cost_per_trade")}
        spec, changed, todo = sx.link_spec(raw, FAKE["/ref/v1/instruments/details/222/CfdOnIndex"])
        self.assertEqual(sorted(changed), ["min_qty", "qty_step", "quote_currency"])
        self.assertEqual((spec["min_qty"], spec["qty_step"], spec["saxo"]["uic"]), (0.1, 0.1, 222))
        self.assertEqual(spec["point_value"], raw["point_value"])        # never taken from Saxo here
        self.assertEqual(todo, [])
        self.assertEqual(spec["_saxo_details"]["ContractSize"], 1)
        _, _, todo = sx.link_spec({}, FAKE["/ref/v1/instruments/details/222/CfdOnIndex"])
        self.assertEqual(todo, ["point_value", "cost_per_trade", "session"])

    def test_cli_link_then_quote_then_bars(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        inst = os.path.join(tmp, "instruments.json")
        shutil.copy(os.path.join(ROOT, "trader-markets", "instruments.example.json"), inst)
        c, gw = client()

        def run(*argv):
            out = io.StringIO()
            with redirect_stdout(out):
                rc = sx.main(["--instruments", inst] + list(argv), client=c, now=NOW)
            return rc, out.getvalue()

        rc, out = run("link", "US500", "--uic", "222", "--asset-type", "CfdOnIndex", "--write")
        self.assertEqual(rc, 0)
        self.assertIn("From Saxo: quote_currency, min_qty, qty_step", out)
        self.assertEqual(mk.load(inst)[1]["US500"]["min_qty"], 0.1)       # markets still reads the file
        prices = os.path.join(tmp, "prices.json")
        rc, out = run("quote", "US500", "--write", prices)
        with open(prices) as fh:
            doc = json.load(fh)
        self.assertEqual(doc["prices"]["US500"], 6800.5)                  # the ask: a long's entry
        self.assertEqual(doc["detail"]["US500"]["delay_minutes"], 15)
        self.assertEqual(doc["detail"]["US500"]["time"], "2026-10-28T13:59:00Z")
        chart = os.path.join(tmp, "US500.json")
        rc, out = run("bars", "US500", "--out", chart)
        self.assertIn("2 bars of 5 minutes, delayed 15 minutes", out)
        with open(chart) as fh:
            r0 = json.load(fh)["chart"]["result"][0]
        q = r0["indicators"]["quote"][0]
        self.assertEqual(r0["timestamp"], [1793107800, 1793108100])
        self.assertEqual((q["open"][0], q["close"][0]), (10, 11))
        self.assertAlmostEqual(q["close"][1], 1.6)                        # FX-style bid/ask -> midpoint
        self.assertEqual(r0["meta"]["dataGranularity"], "5m")
        err = io.StringIO()
        with redirect_stderr(err):
            rc = sx.main(["--instruments", inst, "quote", "NVDA"], client=c, now=NOW)
        self.assertEqual(rc, 2)
        self.assertIn("no Saxo link", err.getvalue())

    def test_stale_or_missing_prices_are_not_used(self):
        for q, why in (({"ask": 1, "price_type_ask": "NoAccess"}, "NoAccess"), ({"ask": None}, "no ask"),
                       ({"ask": 1, "error": "X"}, "error")):
            price, reason = sx.usable_price(q)
            self.assertIsNone(price)
            self.assertIn(why, reason)
        self.assertEqual(sx.usable_price({"ask": 2.5, "price_type_ask": "Indicative"}), (2.5, None))

    def test_chart_limits(self):
        c, _ = client()
        for kw in ({"horizon": 7}, {"count": 1201}, {"count": 0}):
            with self.assertRaises(sx.SaxoError):
                sx.bars(c, 222, "CfdOnIndex", **kw)


if __name__ == "__main__":
    unittest.main()
