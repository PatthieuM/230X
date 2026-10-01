# HW3 Part 2: RIT ALGO1 Write-up

Jean Jacob · Oct 1, 2026

## (a) Strategy going in

My plan was cross-venue arbitrage on CRZY. The stock trades on a Main exchange (CRZY\_M) and an Alternate exchange (CRZY\_A), and RIT nets the position across the two. Whenever the venues crossed, I would buy on the cheap one and sell on the rich one, ending flat in CRZY with the price gap locked in.

| Condition | Action | Profit per share |
| --- | --- | --- |
| CRZY\_M bid > CRZY\_A ask | Sell on Main, buy on Alternate | M bid − A ask |
| CRZY\_A bid > CRZY\_M ask | Sell on Alternate, buy on Main | A bid − M ask |

The case allowed 10,000 shares per order and 25,000 shares of gross and net position, ran for 300 ticks and charged no commissions.

I went into the graded session with the third version of my script, [`algo1_v3.py`](algo1_v3.py). Two test runs earlier the same day had shown me what goes wrong:

| Test version | Execution | What happened |
| --- | --- | --- |
| v1 | Market orders, 1,000 shares per leg, loop every 0.2 s | 125 pairs, **−$39.9k**. 83% of the loss came from re-firing on quotes the server had not refreshed yet, into a book I had just emptied. The other 17% came from 1,000-share orders walking a touch that held only a few hundred shares. When depth was there, fills matched the quotes to the cent, so speed was not the problem. |
| v2 | Marketable limit orders at the touch | Completed pairs earned exactly edge × size (+$40 to +$320 each, +$2,095 realized in about two minutes). But the second leg often filled nothing, legs I had "cancelled" were filled by other traders a moment later (the position drifted to −10,423 shares without the script knowing), and repairs at market walked the thin book (−$340 on 2,000 shares). |

v2 also showed that RIT reports the aggregated CRZY position on both tickers: the Portfolio window showed the same −10,423 twice, so adding the two rows doubles it.

From this I built eight rules into v3:

1. **Limit orders only, priced at the touch.** Nothing I send can fill worse than the quote I saw.
2. **One leg at a time, the fragile leg first.** CRZY\_M, where everybody hedges, loses its quotes first, so its leg goes first. If it fills nothing, the attempt is abandoned at no cost. If it fills, the second leg goes out for exactly the filled quantity.
3. **Read every order back after cancelling it**, so a fill that lands just before the cancel still counts.
4. **The server's position is the truth.** If it shows more than 100 shares net, cancel everything and flatten in limit slices at the touch before trading again.
5. **Never fire twice on the same quote snapshot** (the v1 loss).
6. **Only trade crosses wider than $0.03.** Repairing a missed leg costs about one spread, so the edge has to cover it.
7. **No new pairs after tick 290, and flatten from tick 291**, so the session ends flat.
8. **Log every attempt**, whether filled, abandoned or repaired.

The idea was to give up some fills in exchange for never paying more than the quote and never carrying inventory for long.

## (b) What happened, size strategy, and the script

### What happened

The graded session ran on 10 Sep 2026, and my logs cover 20:02 to 20:20. They contain four runs: three runs of `algo1_v3.py`, plus a second script (`bold`, not in this folder) that I ran for 34 seconds during run 2 and that stopped at its own loss limit. The tables below come from those logs; every fill is in [`all_fills.csv`](all_fills.csv).

**Arbitrage attempts**

| Run | Logged activity (ticks) | Crosses attempted | Abandoned (first leg found nothing) | First leg only | Both legs filled | Edge locked in |
| --- | --- | --- | --- | --- | --- | --- |
| v3, run 1 | 20:02:43–20:02:52 (11–12) | 3 | 0 | 3 | 0 | $0 |
| v3, run 2 | 20:06:38–20:08:11 (32–55) | 3 | 1 | 1 | 1 | $30 |
| bold | 20:07:19–20:07:53 (42–49) | 2 | 1 | 0 | 1 | $150 |
| v3, run 3 | 20:09:09–20:19:39 (69–227) | 40 | 16 | 22 | 2 | $110 |
| **Total** | | **48** | **18** | **26** | **4** | **$290** |

**Fills and repair trades** (a repair trade is a slice sent by the flatten routine)

| Run | Fills | Shares traded | Repair share of volume | Repair fills | Side flips | Repair fills ≥ 5,000 sh | Repair fills of 10,000 sh (order maximum) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| v3, run 1 | 18 | 21,240 | 87.8% | 15 | 7 | 0 | 0 |
| v3, run 2 | 199 | 900,651 | 99.7% | 196 | 60 | 81 | 52 |
| bold | 64 | 291,768 | 96.6% | 62 | 19 | 23 | 14 |
| v3, run 3 | 1,333 | 3,616,215 | 99.3% | 1,307 | 441 | 271 | 128 |
| **Total** | **1,614** | **4,829,874** | **99.2%** | **1,580** | **527** | **375** | **194** |

A side flip is a repair fill on the opposite side from the one before it (a sell slice after a buy slice, or the reverse).

Three things happened:

1. **The arbitrage almost never completed.** On 18 of the 48 crosses, the first leg found nothing and the attempt cost nothing, as designed. On the other 30 the first leg filled, but the second leg filled only 4 times. Sending the legs one after the other was too slow: by the time the first leg had been sent, given up to 50 ms to fill, cancelled and read back, the other venue's quote was usually gone. The 4 completed pairs earned exactly edge × size, $290 in total, so the signal and the price protection worked. The legging did not: 26 attempts left a naked position of up to 1,000 shares.
2. **The repair loop, not the arbitrage, drove the session.** Each broken leg tripped the position guard and handed control to `flatten()`. Repair trades made up 99.2% of the 4.83 million shares I traded, and arbitrage legs only 39,767 shares (0.8%). Run 3 was the longest run, with no other script running. In it, the loop traded 3.59 million shares to close 20,184 shares of broken legs, about 178 repair shares for each broken-leg share.
3. **The repair loop kept overshooting.** `flatten()` reads the position, sends a slice at the touch and reads the position again. Consecutive repair fills were typically only 90 ms apart. So each read came well inside the 30–200 ms during which RIT's position field can still show the old number (Elias [measured this lag](../Elias/algo1_bot/docs/RIT_RULES.md) on the same day). The loop then sent the same slice again and took the position through zero. The first broken leg of run 3 shows the pattern. At 20:10:34.796 the CRZY\_M leg bought 1,000 shares at $10.94 and the CRZY\_A leg missed. Within the next second the loop sold 1,000 shares three times, bought 2,500 twice and sold 3,000 twice: 14,000 shares to close a 1,000-share position. Across run 3, repair trades switched sides 441 times to close 22 broken legs. Because each slice was sized from the position, up to 10,000 shares, the overshoot fed on itself: 180 repair fills in the v3 runs hit the 10,000-share order maximum, ten times the v3 arbitrage clip.

This was v1's failure again, moved from quotes to positions: firing twice on data the server had not refreshed. Rule 5 protected the quote path but not the repair path, and rule 4 was right about the position but not about when the server reports it.

The lesson: in this case, **what happens after a missed leg mattered far more than the signal.** Spotting a cross was easy, and limit orders kept every fill at the quoted price. Control was lost afterwards: the second leg arrived too late, and the repair loop trusted a position the server had not updated yet.

### Size strategy

Each arbitrage clip is the smallest of:

- **Depth at the cross:** the ask size on the buy venue and the bid size on the sell venue. Only shares resting at the crossed prices are worth trading.
- **QUANTITY\_MAX = 1,000 shares**, a tenth of the 10,000 allowed per order. A missed second leg leaves the first leg naked, so this cap bounds the inventory one miss can create.
- **Position headroom:** 20,000 − |net position|, which keeps 5,000 shares of room inside the case limit.

Crosses thinner than MIN\_QUANTITY = 100 shares are skipped. The second leg is sent for exactly what the first leg filled, never the original size. In practice the cap, not the depth, set the size: 20 of the 24 first legs that filled in run 3 were exactly 1,000 shares.

Repair slices were sized differently: the smallest of |net position|, the depth at the touch (at least 100) and 10,000 shares, on the venue with the better price. That was the sizing mistake. The slice came from a position reading that could be stale, and it could be ten times larger than any broken leg it was meant to close. A repair slice should never exceed the broken leg, and the next slice should wait until the position reflects the last one.

### The script ([`algo1_v3.py`](algo1_v3.py))

Each loop, the script:

1. Reads the tick and status from `/case` (at most every 0.25 s) and waits until the case is ACTIVE and at tick 3 or later.
2. Reads both books and the position from `/securities`. RIT shows the aggregated CRZY position on both rows, so `net_position()` reads one row (it adds the two only if they differ).
3. Runs the position guard: if |net| > 100 shares, it cancels all orders and runs `flatten()` before anything else.
4. Skips the loop if the quotes haven't changed since the last attempt.
5. Checks both crosses against MIN\_EDGE and sizes the clip.
6. Runs `attempt()`, which sends the CRZY\_M leg as a limit order at the touch. `settle()` lets the order fill for up to 50 ms, cancels the rest and reads the order back. If nothing filled, the attempt is abandoned. Otherwise the other leg goes out for the filled quantity and is settled the same way. If it comes up short, up to 6 repair slices run straight away.
7. Writes every attempt and repair slice to `logs/trades_v3_<timestamp>.csv`.

After tick 290 it opens no new pairs, and at tick 291 it cancels everything and flattens.

[`flatten.py`](flatten.py) is the same clean-up as a stand-alone script. It cancels all open orders, then flattens the aggregated CRZY position in up to 60 limit slices at the touch. Each slice rests 0.15 s and is then cancelled.

**How to use it**

1. Install the dependency: `pip install requests`.
2. Set the connection. Either create `rit_config.py` next to the scripts:

   ```python
   BASE_URL = "http://<host>:<port>/v1"  # DMA REST API address from the client's API Info dialog, plus /v1
   USER = "<trader id>"
   PASSWORD = "<password>"
   # Client REST API instead: BASE_URL = "http://localhost:<port>/v1", USER = None, API_KEY = "<key>"
   ```

   or set the environment variables `RIT_BASE_URL`, `RIT_USER`, `RIT_PASSWORD` or `RIT_API_KEY`, which take precedence over the file. With a user name, the scripts use basic authentication; otherwise they send the API key in the `X-API-Key` header. `rit_config.py` holds a password, so it is git-ignored and not in this folder.
3. Log in to RIT, load ALGO1 and run `python algo1_v3.py`. It waits for the case to start.
4. Watch the console. Each attempt prints one line: either both legs with the edge, matched size and pair P&L, or "gone, abandoned". Position-guard and flatten lines appear in between.
5. Stop it with Ctrl+C, pressed once; the script finishes its current loop. If Trader Info doesn't then show a zero position, run `python flatten.py`.

| Setting | Final value | What it does |
| --- | --- | --- |
| MIN\_EDGE | 0.03 | Minimum cross ($/share); it must cover the roughly one-spread cost of repairing a missed leg |
| QUANTITY\_MAX | 1,000 | Largest arbitrage clip |
| MIN\_QUANTITY | 100 | Smallest clip worth sending |
| LEG\_ORDER | "M" | Which leg goes first: "M" or "A" (venue), "thin" (thinner side), "buy" or "sell" |
| FILL\_WAIT | 0.05 s | How long a leg may rest before its remainder is cancelled |
| MAX\_POSITION | 20,000 | Position cap used in sizing (case limit 25,000) |
| POSITION\_TOLERANCE | 100 | Net position above which the guard cancels everything and flattens |
| REPAIR\_TRIES | 6 | Repair slices tried right after a short second leg |
| STOP\_TICK / FLATTEN\_TICK | 290 / 291 | Last tick for new pairs / first tick of the end-of-session flatten |

## (c) Market vs. limit orders

A market order trades **price certainty for execution certainty**, and a limit order does the reverse. In a two-legged arbitrage, that choice decides which risk you carry: slippage on every trade, or a naked leg when one side misses.

- **Market order:** executes at once against whatever rests in the book, at whatever prices it reaches. The fill is certain, but the price is not.
- **Limit order:** executes only at its price or better. Priced at the touch, it is marketable: it trades at once against the shares resting at that price, and any remainder rests until it fills or is cancelled. The price is capped, but the fill is not certain.

|  | Market orders | Limit orders at the touch |
| --- | --- | --- |
| Fill | Certain, up to the depth of the book | Only what rests at my price when the order arrives |
| Price | Not capped: a large order walks the book | Never worse than my limit |
| Main risk | **Slippage.** In a thin book the order walks past the touch. If the quote was stale, it fills against whatever is left, and the "arbitrage" can lose on both legs | **Legging.** One leg fills and the other doesn't, leaving a naked position whose P&L depends on the price path until it is closed |
| Other risks | No cap on the cost once the order is sent. Re-firing on a quote the server hasn't refreshed sends the order into a book you just emptied | A remainder can fill after you decide to cancel it. Resting orders get picked off when the price moves through them. Closing a naked leg costs about one spread, more if done at market |
| Worst case in a two-leg arbitrage | Both legs fill, but each can fill past the cross | A missed first leg costs nothing. A missed second leg leaves an open-ended position until it is repaired |

**What I saw in practice.** I tried both, and each failed the way the table predicts:

- **Market orders (v1, test):** −$39.9k on 125 pairs, mostly from firing on stale quotes and walking thin touches. Slippage, not latency, was the cost.
- **Marketable limit orders (v2, test):** the price protection worked, since every completed pair earned exactly edge × size. But missed second legs, late fills on "cancelled" legs and repairs at market left me with inventory I did not want.
- **Limit orders, one leg at a time (v3, graded):** abandoned attempts cost nothing and completed pairs earned exactly what the quotes promised, but only 4 of 30 pairs completed. The 26 broken legs handed the session to the repair loop, which did 99% of my trading.

The trade-off doesn't go away. Market legs turn legging risk into slippage, which can be bounded: keep each order no larger than the touch, set MIN\_EDGE above typical slippage, and never fire twice on the same snapshot. Limit legs cap the price but leave inventory risk, and the result is only as good as the repair process. Sending the limit legs one at a time made that worse, because the second leg arrived after the cross was gone.

## (d) P&L

My graded P&L, as recorded by the TA, was **$66,296.29**.

| Metric | Value |
| --- | --- |
| Graded P&L (TA's record) | **$66,296.29** |
| Crosses attempted | 48 |
| Completed pairs (both legs filled) | 4 |
| Edge locked in by completed pairs | $290 |
| Broken legs (first leg only) | 26 |
| Shares traded | 4,829,874, of which 99.2% were repair trades |

The result is positive, but it is not arbitrage profit. The four completed pairs account for $290. The other $66,000 came from everything else my scripts traded: the broken legs and the repair trades around them. Meanwhile my fill prices went from about $10.1 at the start to $13.08 around 20:15 and back to $10.43 by 20:19. The v3 position guard repeatedly saw positions of 10,000 shares or more, which is what sized its 180 slices at the 10,000-share maximum. A $3 move on 10,000 shares is worth $30,000 either way. That is price exposure, not a locked-in spread, and the same exposure could just as easily have produced a loss of that size.

I also rebuilt a running P&L from my trade logs, but it does not match the graded figure, so I don't report it. The logs record what each script read back for its own orders; they are not an account statement. I use them only for counts and sizes, here and in `all_fills.csv`, whose `cash` and `run` columns come from that reconstruction.

What I would change next time:

1. **Send both legs at once.** Sequential legs completed 4 times in 30. Firing both as limit orders at the touch at the same moment keeps the price cap and gives the second leg a chance before the cross disappears.
2. **Make the repair loop wait for the position.** Rule 5 should cover positions as well as quotes. After a slice fills, the next one should wait until the server's position reflects it, and no slice should be larger than the broken leg it closes.
3. **Run one script per account at a time**, so no position guard trades another script's inventory.
4. **Log the server's NLV (`/trader`) during the run**, so the P&L in the write-up comes from the account rather than from a reconstruction.

## Files in this folder

| File | What it is |
| --- | --- |
| `RIT_ALGO1_Writeup_Jean.md` | This write-up |
| [`algo1_v3.py`](algo1_v3.py) | The arbitrage script used in the graded session |
| [`flatten.py`](flatten.py) | Stand-alone clean-up: cancels all orders and flattens the CRZY position at the touch |
| [`all_fills.csv`](all_fills.csv) | All 1,614 fills from the four logs, in time order |

`all_fills.csv` columns:

- `time` and `t`: the timestamp, in two formats.
- `tick`
- `type`: `PAIR` is a first leg, `PAIR2` a second leg and `FLATTEN` a repair slice.
- `ticker`, `action`
- `qty`: shares filled.
- `price`: the order's fill price (VWAP).
- `signed`: shares, positive for a buy and negative for a sell.
- `cash`: the fill's cash flow.
- `src`: the source log.
- `run`: the running P&L rebuilt from the logs. It does not match the graded P&L; see (d).
