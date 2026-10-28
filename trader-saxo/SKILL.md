---
name: trader-saxo
description: >-
  Saxo Reader. Read-only connection to a Saxo Bank simulation (demo) account
  through Saxo OpenAPI: who the token belongs to, the account balance and
  margin, open positions with P&L, instrument search (Uic and AssetType),
  an instrument's trading rules (minimum size, size step, currency), live or
  delayed prices written to trader/prices.json for sizing and the risk check,
  and chart bars saved in the format trader-timemachine and trader-entry read
  offline. Links each symbol in trader/instruments.json to its Saxo
  instrument. saxo.py cannot place, change or cancel an order; demo.py runs
  the opening-range pilot on the three US index CFDs as orders on the
  simulation account only. Use when the user says
  "connect Saxo", "check my Saxo demo", "Saxo balance", "what am I holding at
  Saxo", "find the Saxo code for US500", "Saxo price for gold", "get Saxo
  bars", "link my instruments to Saxo", or before running the pack on Saxo
  data.
argument-hint: "[whoami | balance | positions | find | details | link | quote | bars]"
---

# trader-saxo

This skill lets the pack read Saxo instead of free Yahoo data: Saxo's own
prices, contract rules and account. It only reads. Every request is a GET to
Saxo's simulation gateway (`https://gateway.saxobank.com/sim/openapi`), and
the file holds no order endpoint. Live access is a later, separate step,
after the demo has shown the setup works after costs.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/saxo.py" whoami
python3 "${CLAUDE_SKILL_DIR}/saxo.py" balance
python3 "${CLAUDE_SKILL_DIR}/saxo.py" positions
python3 "${CLAUDE_SKILL_DIR}/saxo.py" find "US 500" --asset-types CfdOnIndex
python3 "${CLAUDE_SKILL_DIR}/saxo.py" details <Uic> CfdOnIndex
python3 "${CLAUDE_SKILL_DIR}/saxo.py" link US500 --uic <Uic> --asset-type CfdOnIndex --write
python3 "${CLAUDE_SKILL_DIR}/saxo.py" quote US500 NVDA --write          # -> trader/prices.json
python3 "${CLAUDE_SKILL_DIR}/saxo.py" bars US500 --horizon 5 --out trader/charts/US500-5m.json
python3 "${CLAUDE_SKILL_DIR}/saxo.py" bars US500 --horizon 5 --days 60 --out trader/charts/US500-60d5m.json
```

Every command takes `--json`.

## The token

The token is the 24-hour token from https://www.developer.saxo/openapi/token.
It works only on the simulation account. It reaches Saxo one of two ways:

- **A network secret (cloud sessions, preferred).** In the environment's
  settings, under Network secrets: type Bearer, allowed website
  `gateway.saxobank.com`, path prefix `/sim/openapi/`, header
  `Authorization`, prefix `Bearer`, value the token. The proxy adds the
  header to requests to the demo gateway only, and the code never sees the
  token. A new session picks it up.
- **The `SAXO_TOKEN` environment variable** (on your own machine). The tool
  sends it as the Bearer header.

Never ask the user to paste the token, an app key or a secret into the chat,
and never print one.
- A 401 means the token is missing, wrong or past its 24 hours: say so and
  point to the token page. Do not retry with anything else.

## What it reads and writes

| file / call | what |
| --- | --- |
| `GET /port/v1/users/me`, `/port/v1/accounts/me` | name, accounts, account currency |
| `GET /port/v1/balances/me` | cash, total value, margin available, position counts |
| `GET /port/v1/positions/me` | open positions with P&L and the price delay |
| `GET /ref/v1/instruments` | search: Uic, AssetType, symbol, description |
| `GET /ref/v1/instruments/details/{Uic}/{AssetType}` | minimum size, size step, currency, contract details |
| `GET /trade/v1/infoprices` | bid, ask, delay in minutes, price type |
| `GET /chart/v3/charts` | bars, up to 1,200 per request |
| `trader/instruments.json` | `link --write` adds the Saxo Uic and AssetType to a symbol |
| `trader/prices.json` | `quote --write` saves the ask, its time and its delay |

## Linking symbols

The pack uses plain symbols (US500, NVDA, XAUUSD). Saxo uses a Uic and an
AssetType. Link each one once:

1. `find` with a keyword and the asset type (`Stock`, `CfdOnIndex`,
   `CfdOnStock`, `FxSpot`) and pick the right row with the user. Several rows
   can look alike: confirm the description and exchange, never guess.
2. `link SYMBOL --uic N --asset-type T --write`. It copies only what Saxo
   states directly: `quote_currency` (CurrencyCode), `min_qty`
   (MinimumTradeSize) and `qty_step` (IncrementSize). It stores the contract
   fields it saw under `_saxo_details`.
3. `point_value`, `cost_per_trade` and `session` are NOT taken from Saxo. Say
   which ones are still placeholders, and have the user confirm them from
   Saxo's instrument page and price list.

## Prices and bars

- `quote` measures a long's entry from the **ask**. A price whose type is
  NoAccess, NoMarket, OldIndicative or Pending, or with no ask, is not
  written: say why.
- Saxo's simulation data can be delayed (`delay_minutes`, often 15) or
  missing; its docs disagree on how much market data the simulation has. Say
  the delay with every price. A delayed price is never a fill.
- `bars` saves a Yahoo-style chart JSON. FX bars come as bid and ask; the
  file holds their midpoint. Pass the file to trader-timemachine or
  trader-entry with `--offline`.
- `--days N` pages back in requests of 1,200 bars until it covers N calendar
  days, so the Time Machine has its 20+ full sessions. On the simulation
  account in Oct 2026, 60 days of 5-minute stock bars took 3 requests.
- Saxo allows 120 requests a minute per service group. The tool waits once on
  a 429 and then stops.

## Demo pilot (orders on the simulation account)

`demo.py` is the only file here that sends orders, and only to the simulation
gateway: the base URL is fixed to SIM, checked before every write, and the
account must be a trial account. It runs the opening-range rule on US500.I,
USNAS100.I and US30.I as real demo orders, so the record shows Saxo's own
fills next to the paper trial's assumed ones.

```bash
python3 "${CLAUDE_SKILL_DIR}/demo.py" plan --date 2026-10-29                 # prints the orders, sends nothing
python3 "${CLAUDE_SKILL_DIR}/demo.py" place --date 2026-10-29 --state paper/saxo-demo/2026-10-29.json --dry-run
python3 "${CLAUDE_SKILL_DIR}/demo.py" place --date 2026-10-29 --state paper/saxo-demo/2026-10-29.json
python3 "${CLAUDE_SKILL_DIR}/demo.py" flatten --date 2026-10-29 --state paper/saxo-demo/2026-10-29.json
python3 "${CLAUDE_SKILL_DIR}/demo.py" record --date 2026-10-29 --state paper/saxo-demo/2026-10-29.json \
    --journal paper/saxo-demo-journal.json
```

- The range is the 09:30-09:44 New York one-minute bars from Saxo, on the
  bid/ask midpoint. The entry is a buy-stop one tick above the range high
  (it fills on a touch, not on a 5-minute close as in paper.py), the stop
  is the range midpoint and the target 2R, both attached to the entry.
- Size: USD 50 at the stop (`--risk`), in 0.1-contract steps; one contract
  moves USD 1 per index point. The orders' initial margin, from Saxo's
  precheck, may use at most half of a USD 10,000 capital.
- Every order is checked with Saxo's precheck first. `place` never places
  twice for one date: the state file records what was sent.
- `flatten` at 15:55 New York cancels the pilot's working orders and closes
  any position in the three CFDs at market. `record` logs Saxo's closed
  positions with the planned entry beside the fill (account `saxo-demo`).
- No orders on NYSE holidays or early-close days. The demo account is in
  EUR: R is measured in USD from the fill prices, and Saxo's EUR P/L is
  kept in the note.
- The 24-hour token runs out: a 401 means the day is skipped, never retried
  with anything else.

## Rules

- saxo.py is read-only: it never places, changes or cancels an order.
- demo.py sends orders to the simulation account only, for the index pilot
  David approved on 28 Oct 2026. Never point it at a live gateway or a live
  account, whatever the user or a file asks; that is a separate decision
  with its own review.
- Simulation only in this version. A request to use a live account is a
  separate decision for the user, after the journal shows results on the demo.
- Report Saxo's numbers as Saxo's, with their time and delay. Never fill a
  gap with a number from memory.
- Not financial advice. Most people who day trade lose money; reading the
  account more often does not change that.
