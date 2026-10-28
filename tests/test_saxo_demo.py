import contextlib
import io
import json
import os
import tempfile
import unittest

from _load import load

demo = load("trader-saxo", "demo")
DAY = "2026-10-27"       # New York is on UTC-4 that day: 09:30 is 13:30Z


def minute_bars(day_utc_hour=13, start_min=30, n=15, base=7700.0):
    rows = []
    for i in range(n):
        m = start_min + i
        t = "2026-10-27T%02d:%02d:00Z" % (day_utc_hour + m // 60, m % 60)
        mid = base + i
        rows.append({"Time": t, "OpenBid": mid - 0.5, "OpenAsk": mid + 0.5, "HighBid": mid + 1.5, "HighAsk": mid + 2.5,
                     "LowBid": mid - 2.5, "LowAsk": mid - 1.5, "CloseBid": mid - 0.5, "CloseAsk": mid + 0.5})
    return {"Data": rows}


class Rule(unittest.TestCase):
    def test_range_uses_only_the_first_fifteen_minutes(self):
        chart = minute_bars()
        chart["Data"].append({"Time": "2026-10-27T13:45:00Z", "HighBid": 9000, "HighAsk": 9001,
                              "LowBid": 1, "LowAsk": 2, "OpenBid": 1, "OpenAsk": 2, "CloseBid": 1, "CloseAsk": 2})
        hi, lo = demo.opening_range(chart, DAY)
        self.assertEqual((hi, lo), (7716.0, 7698.0))

    def test_an_incomplete_range_is_refused(self):
        with self.assertRaises(demo.DemoError):
            demo.opening_range(minute_bars(n=14), DAY)

    def test_plan_sizes_to_the_risk_and_puts_the_target_at_2r(self):
        p = demo.plan_trade(7716.0, 7698.0, 50)
        self.assertEqual((p["entry"], p["stop"], p["target"]), (7716.01, 7707.0, 7734.03))
        self.assertEqual(p["amount"], 5.5)                     # 50 / 9.01 = 5.55 -> 5.5
        self.assertLessEqual(p["risk_usd"], 50)

    def test_the_order_has_its_stop_and_target_attached(self):
        body = demo.order_body("acc", 4913, demo.plan_trade(7716.0, 7698.0, 50))
        self.assertEqual((body["OrderType"], body["BuySell"], body["OrderPrice"]), ("StopIfTraded", "Buy", 7716.01))
        legs = {o["OrderType"]: o for o in body["Orders"]}
        self.assertEqual((legs["StopIfTraded"]["OrderPrice"], legs["Limit"]["OrderPrice"]), (7707.0, 7734.03))
        self.assertFalse(body["ManualOrder"])

    def test_range_end_is_0945_new_york(self):
        self.assertEqual(demo.range_end_utc("2026-10-27"), "2026-10-27T13:45:00Z")
        self.assertEqual(demo.range_end_utc("2026-11-02"), "2026-11-02T14:45:00Z")   # after the clocks change

    def test_no_session_days(self):
        self.assertIn("weekend", demo.no_session("2026-10-31"))
        self.assertIn("holiday", demo.no_session("2026-11-26"))
        self.assertIn("early close", demo.no_session("2026-11-27"))
        self.assertIsNone(demo.no_session(DAY))


class SimOnly(unittest.TestCase):
    def test_a_live_gateway_is_refused(self):
        with self.assertRaises(demo.DemoError):
            demo.Trader(token="x", base="https://gateway.saxobank.com/openapi")


class Fake:
    """Stands in for the Trader: canned GETs, and every write recorded, never sent."""

    def __init__(self, gets):
        self.gets, self.sent = gets, []

    def get(self, path, params=None):
        for key, val in self.gets.items():
            if path.startswith(key):
                return val
        raise AssertionError("unexpected GET " + path)

    def send(self, method, path, body=None, params=None):
        self.sent.append((method, path, body, params))
        if path.endswith("/precheck"):
            return {"PreCheckResult": "Ok", "InstrumentToAccountConversionRate": 0.9,
                    "MarginImpactBuySell": {"InitialMarginBuy": 90.0}}
        return {"OrderId": 11, "Orders": [{"OrderId": 12}, {"OrderId": 13}]}


ACCOUNTS = {"Data": [{"AccountKey": "acc", "IsTrialAccount": True}]}


def run(argv, client):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = demo.main(argv, client=client)
    return code, out.getvalue()


class Commands(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.tmp.name, "state.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_dry_run_prechecks_and_sends_no_order(self):
        c = Fake({"/port/v1/accounts/me": ACCOUNTS, "/chart/v3/charts": minute_bars()})
        code, text = run(["place", "--date", DAY, "--state", self.state, "--dry-run"], c)
        self.assertEqual(code, 0)
        self.assertTrue(all(p.endswith("/precheck") for _, p, _, _ in c.sent))
        self.assertIn("dry run, not sent", text)
        self.assertFalse(os.path.exists(self.state))

    def test_place_saves_the_order_ids_and_never_places_twice(self):
        c = Fake({"/port/v1/accounts/me": ACCOUNTS, "/chart/v3/charts": minute_bars()})
        run(["place", "--date", DAY, "--state", self.state], c)
        placed = [s for s in c.sent if s[1] == "/trade/v2/orders"]
        self.assertEqual(len(placed), 3)
        with open(self.state) as fh:
            self.assertEqual(json.load(fh)["plans"]["US500"]["order_id"], 11)
        c2 = Fake({"/port/v1/accounts/me": ACCOUNTS, "/chart/v3/charts": minute_bars()})
        code, text = run(["place", "--date", DAY, "--state", self.state], c2)
        self.assertEqual(c2.sent, [])
        self.assertIn("already placed", text)

    def test_margin_over_budget_is_skipped(self):
        c = Fake({"/port/v1/accounts/me": ACCOUNTS, "/chart/v3/charts": minute_bars()})
        code, text = run(["place", "--date", DAY, "--state", self.state, "--capital", "150"], c)
        self.assertEqual(len([s for s in c.sent if s[1] == "/trade/v2/orders"]), 0)
        self.assertIn("budget", text)

    def test_a_demo_account_is_required(self):
        c = Fake({"/port/v1/accounts/me": {"Data": [{"AccountKey": "acc", "IsTrialAccount": False}]},
                  "/chart/v3/charts": minute_bars()})
        code, _ = run(["place", "--date", DAY, "--state", self.state], c)
        self.assertEqual(code, 2)
        self.assertEqual(c.sent, [])

    def test_flatten_cancels_orders_and_closes_positions(self):
        c = Fake({"/port/v1/accounts/me": ACCOUNTS,
                  "/port/v1/orders/me": {"Data": [{"OrderId": 12, "Uic": 4913, "AssetType": "CfdOnIndex"},
                                                  {"OrderId": 99, "Uic": 21, "AssetType": "FxSpot"}]},
                  "/port/v1/netpositions/me": {"Data": [{"NetPositionBase": {"Uic": 4913, "AssetType": "CfdOnIndex",
                                                                             "Amount": 5.5}}]}})
        run(["flatten", "--date", DAY], c)
        self.assertEqual(c.sent[0][:2], ("DELETE", "/trade/v2/orders/12"))
        close = c.sent[1][2]
        self.assertEqual((close["BuySell"], close["Amount"], close["OrderType"]), ("Sell", 5.5, "Market"))

    def test_record_logs_the_fill_against_the_plan(self):
        plan = demo.plan_trade(7716.0, 7698.0, 50)
        with open(self.state, "w") as fh:
            json.dump({"plans": {"US500": dict(plan, uic=4913)}}, fh)
        journal_path = os.path.join(self.tmp.name, "j.json")
        c = Fake({"/port/v1/closedpositions/me": [{"ClosedPosition": {
            "Uic": 4913, "AssetType": "CfdOnIndex", "Amount": 5.5, "OpenPrice": 7716.5, "ClosingPrice": 7734.03,
            "ExecutionTimeClose": "2026-10-27T15:10:00Z", "ProfitLossOnTradeInBaseCurrency": 86.6}}]})
        code, text = run(["record", "--date", DAY, "--state", self.state, "--journal", journal_path], c)
        self.assertEqual(code, 0)
        with open(journal_path) as fh:
            t = json.load(fh)["trades"][0]
        self.assertEqual((t["account"], t["entry"], t["stop"]), ("saxo-demo", 7716.5, 7707.0))
        self.assertIn("filled 7716.50 (+0.49)", t["note"])


if __name__ == "__main__":
    unittest.main()
