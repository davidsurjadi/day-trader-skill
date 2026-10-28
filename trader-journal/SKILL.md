---
name: trader-journal
description: >-
  Trade Journal. Records every closed trade and says, per setup, whether the
  record shows an edge after costs. Each trade is measured in R (the result
  divided by the risk at the stop), before and after commission, spread and
  financing. Works for US shares, FX lots, gold and index CFDs through each
  instrument's point value, long or short. Imports the fills trader-broker
  logged, never twice. Stats per setup, symbol, asset class or account: win
  rate with its 95% interval, average R with its 95% interval, expectancy,
  profit factor, deepest drawdown and longest losing run, with one plain
  verdict. Use when the user says "log this trade", "journal", "how am I
  doing", "is this setup working", "what's my win rate", "am I making money
  after costs", "review my trades", "weekly review", or after a trade closes.
argument-hint: "[add | import | stats] [--by setup|symbol|asset_class|account]"
---

# trader-journal

The other skills find, time, size and check a trade. None of them can say
whether the setup is worth trading. The journal can, once there is a record.
It logs each closed trade once, in money and in R, and its stats show what
that record does and does not prove.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/journal.py" add --symbol TSLA --setup orb --side long \
    --entry 364.18 --stop 358.32 --exit 370.04 --qty 8 --costs 2.00 --date 2026-10-19
python3 "${CLAUDE_SKILL_DIR}/journal.py" import --setup orb          # trader-broker's fills
python3 "${CLAUDE_SKILL_DIR}/journal.py" list
python3 "${CLAUDE_SKILL_DIR}/journal.py" stats                       # per setup
python3 "${CLAUDE_SKILL_DIR}/journal.py" stats --by symbol --account demo
python3 "${CLAUDE_SKILL_DIR}/journal.py" stats --since 2026-11-01 --json
```

## What it reads and writes

| file | what |
| --- | --- |
| `trader/journal.json` | the journal: one record per closed trade (written by this skill) |
| `trader/instruments.json` | point value, asset class and default cost per instrument (optional) |
| `trader/state/broker.json` | trader-broker's fills and approved tickets (read by `import`) |
| `trader/plan.json` | the setup name, when `import` has no `--setup` and the symbol matches |

Copy `instruments.example.json` to `trader/instruments.json` and replace every
value with the broker's own contract specification. The example's costs are
zeros on purpose: a journal that ignores costs flatters every setup.

## How a trade is measured

- **1R** is the money lost if the trade had stopped out at its original stop:
  `|entry - stop| x qty x point_value`. Moving a stop later does not change 1R.
- **Gross** is `(exit - entry) x direction x qty x point_value`.
- **Net** is gross minus costs (commission both sides, spread, financing).
- **R** is gross or net divided by 1R. A trade with no stop, or a stop on the
  wrong side of the entry, is refused: it has no R.
- **point_value** is money per 1.0 move in the price per 1 unit of qty: 1 for a
  share, the contract size for a lot (for example 100,000 units of EUR/USD in
  USD). It comes from `--point-value`, else `trader/instruments.json`, else 1.
- Each trade gets an id from its contents, so adding or importing the same
  trade twice does nothing.

`import` pairs trader-broker's filled buys and sells per stock, oldest first
(the pack trades long only). It takes 1R from the stop on that day's
approved entry ticket. A round trip with no recorded stop is skipped and
listed, never guessed. Positions still open are listed and left out.

## Reading the stats

| field | meaning |
| --- | --- |
| win rate, 95% interval | share of trades that made money after costs; the Wilson interval stays honest at small samples |
| average R, 95% interval | the mean result per trade in R, before and after costs; the interval is the normal approximation |
| expectancy | average net money per trade |
| costs, share of gross | how much of the gross profit the costs ate |
| profit factor | money won divided by money lost |
| deepest drawdown | the biggest fall in cumulative R from a peak |
| longest losing run | the most losses in a row |

The verdict reads only the 95% interval of average R after costs:

- whole interval above zero: the record shows an edge after costs;
- whole interval below zero: the record shows a loss after costs;
- interval includes zero: no edge shown yet.

Say the verdict plainly, including the interval. Do not round "no edge shown"
up to "promising". A wide interval means the sample is small, not that the
setup works. Keep paper, demo and live trades apart with `--account`, and
compare them with `stats --by account`: a setup that only works on paper
usually has a cost or fill problem.

## How to run it

1. After a trade closes, record it: `import` if trader-broker logged the fills,
   else `add` with the numbers from the broker's statement.
2. For a review, run `stats` (per setup), then `stats --by symbol` to see
   whether one instrument carries the result.
3. Report: trades, win rate with its interval, average R after costs with its
   interval, what the costs took, the drawdown, and the verdict.
4. If costs take a large share of the gross profit, say so first. Cutting
   costs or trading a wider target is a decision for the user.

## Paper forward test (US500)

`paper.py` applies ONE fixed rule to one New York session and logs the result
as a paper trade (`account: paper`, setup `orb15-mid-2R`). The rule is written
in the file so it cannot drift: 15-minute opening range, entry on the first
5-minute close above the range high, stop at the range midpoint, target 2R,
stop first when one bar touches both, flat by 15:55. It reads Yahoo's ^GSPC
5-minute bars by default (Saxo's US500.I tracks the same index; its price
level can differ a little) or any saved Yahoo-style chart, and charges a
round-trip cost in points (default 0.6, Saxo Classic's US500.I spread).

```bash
python3 "${CLAUDE_SKILL_DIR}/paper.py" run --date 2026-10-28              # log yesterday's session
python3 "${CLAUDE_SKILL_DIR}/paper.py" run --date 2026-10-28 --dry-run    # show only
python3 "${CLAUDE_SKILL_DIR}/journal.py" stats --account paper            # the forward record
```

It logs qty 1 at point value 1, so R is exact whatever the contract size.
Never backfill past sessions into the forward record: it starts on the day the
trial starts. Read it with the verdict rules above; do not change the rule
mid-trial.

## Rules

- Never invent a trade, a fill, a stop or a cost. If a number is missing, ask.
- Never edit a logged trade in place to make the record look better. A wrong
  entry is fixed by the user, in `trader/journal.json`, with a note.
- A record of past trades is not a forecast. Most people who day trade lose
  money; a good record over a few trades does not change that.
- Not financial advice. The journal places nothing and never leads to an order.
