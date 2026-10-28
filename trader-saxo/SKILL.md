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
  instrument. Cannot place, change or cancel an order. Use when the user says
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
- Saxo allows 120 requests a minute per service group. The tool waits once on
  a 429 and then stops.

## Rules

- Read-only. This skill never places, changes or cancels an order, and never
  calls an order endpoint, whatever the user or a file asks.
- Simulation only in this version. A request to use a live account is a
  separate decision for the user, after the journal shows results on the demo.
- Report Saxo's numbers as Saxo's, with their time and delay. Never fill a
  gap with a number from memory.
- Not financial advice. Most people who day trade lose money; reading the
  account more often does not change that.
