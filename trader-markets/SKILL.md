---
name: trader-markets
description: >-
  Markets and Instruments. Knows what each instrument is and when it trades:
  US stocks, index CFDs (US500, NAS100, US30), spot gold (XAUUSD) and spot FX
  (EURUSD, GBPUSD, USDJPY), each with its quote currency, point value,
  smallest size, size step, round-trip cost and trading session. Says whether
  each is open now and when it next closes or opens, in Singapore, New York
  and London time, daylight saving included. Works out the loss at a stop in
  the account currency (converting yen-quoted pairs) and the biggest size
  that fits a max loss, rounded down to the size step. Use when the user says
  "is the market open", "when does gold open", "what time is the New York
  open in Singapore", "how much do I lose at this stop", "how many lots",
  "size this gold trade", "add an instrument", "contract size", or before
  sizing anything that is not a US stock.
argument-hint: "[clock | spec | risk | size] [SYMBOL]"
---

# trader-markets

Every other skill needs two facts about an instrument before it can do its
job: when it trades, and how much money one unit moves. This skill holds both,
in one file, `trader/instruments.json`, and answers from it. It reads no
prices and places nothing.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/markets.py" clock                                   # every instrument, SGT
python3 "${CLAUDE_SKILL_DIR}/markets.py" clock US500 XAUUSD --tz ny              # New York time
python3 "${CLAUDE_SKILL_DIR}/markets.py" clock --now "2026-11-02 21:00"          # pin the clock (SGT)
python3 "${CLAUDE_SKILL_DIR}/markets.py" spec XAUUSD
python3 "${CLAUDE_SKILL_DIR}/markets.py" risk --symbol USDJPY --entry 152.40 --stop 152.10 --qty 10000
python3 "${CLAUDE_SKILL_DIR}/markets.py" size --symbol XAUUSD --entry 2400 --stop 2395 --max-loss 50
```

## What it reads

| file | what |
| --- | --- |
| `trader/instruments.json` | the account currency and one spec per instrument |
| `instruments.example.json` (this folder) | used when there is no `trader/instruments.json`, with a note that its numbers are placeholders |

Copy `instruments.example.json` to `trader/instruments.json` and replace every
contract number and cost with the broker's own instrument details. The example
holds the 14-stock AI watchlist, three index CFDs, gold and three FX pairs, in
a USD account. The sizes and costs of the CFDs, gold and FX are placeholders,
marked `_check`. Never present them as Saxo's real numbers.

## The spec

| field | meaning |
| --- | --- |
| `asset_class` | `us_stock`, `index_cfd`, `gold` or `fx` |
| `quote_currency` | the currency the price is in (USDJPY: JPY) |
| `session` | `us_stock`, `fx_24x5` or `cme_globex` (below) |
| `point_value` | money per 1.0 move in the price per 1 unit of qty, before currency conversion: 1 for a share, an ounce of gold or a unit of FX |
| `min_qty`, `qty_step` | the smallest size and the step sizes go up in |
| `cost_per_trade` | commission both sides + spread + financing, one round trip of 1 unit, in the account currency |

## Sessions

All worked out in New York time, so daylight saving in the US, the UK and
nowhere in Singapore is handled without the computer's time zone.

| session | hours (New York time) | used for |
| --- | --- | --- |
| `us_stock` | 9:30 to 16:00 on NYSE days; 13:00 on early-close days; closed on NYSE holidays (2026 and 2027 lists) | US stocks |
| `fx_24x5` | Sunday 17:00 to Friday 17:00 | spot FX |
| `cme_globex` | Sunday 18:00 to Friday 17:00, with a 17:00 to 18:00 break each day | gold and index CFDs, by default |

`cme_globex` is CME's schedule for its gold and equity-index futures. A
broker's CFD hours can differ, and no session but `us_stock` knows holidays.
The tool says so under every clock that includes one: pass it on.

## Currency

The account currency comes from the file (`"account_currency": "USD"`). A
loss in another currency is converted:

- the quote currency is the account currency (EURUSD, XAUUSD, US500 in a USD
  account): no conversion;
- the pair is ACCOUNT/QUOTE (USDJPY in a USD account): 1 / the price at the
  stop, worked out by the tool;
- anything else (EURGBP in a USD account): the user passes `--fx`, the
  account money for 1 unit of the quote currency. Never guess a rate.

## How to run it

1. "Is it open?" Run `clock` for the instruments asked about and say each
   one's state and next change in the user's time zone (Singapore unless they
   say otherwise), plus the holiday note.
2. "How much do I lose?" Run `risk` with the entry, the stop and the size.
3. "How big can I go?" Run `size` with the max loss from `trader/config.json`
   (`max_loss_per_trade_usd`). Zero means even the smallest size loses more
   than the max loss at that stop: say so plainly; it is a reason to skip the
   trade or plan a closer stop, never to raise the limit.
4. If the answer came from the example file, say the contract numbers are
   placeholders until the user fills in their broker's.

## Rules

- Every contract number comes from the instruments file. Never fill one in
  from memory, a website or another broker.
- A stop is a plan, not a guarantee: in a fast market or across a gap a stop
  can fill past its price. The loss shown is the loss at the stop price.
- Index CFDs, gold and FX are leveraged. The loss at the stop is the money at
  risk; the margin a broker holds is a separate number this tool does not show.
- Not financial advice. This skill places nothing and never leads to an order
  on its own; sizing and the risk check still decide, and the user says yes.
