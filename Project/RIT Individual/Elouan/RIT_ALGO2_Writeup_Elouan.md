# Final Project Part 2.1: RIT ALGO2 Market Making Write-up

Elouan Bahri · Individual write-up · Oct 5, 2026

Script: [`algo2_momentum.py`](algo2_momentum.py). My first, passive version is [`algo2_market_making.py`](algo2_market_making.py). My ALGO2e scripts for the group case are in [`algo2e/`](algo2e/) (see the last section).

## Setup and economics

ALGO2 has one stock (ALGO), traded for 300 seconds. Orders are capped at 5,000 shares, and positions at 25,000 shares gross or net, with a 10¢-per-share fine beyond that. Market orders pay a 1¢ commission; filled limit orders receive a ½¢ rebate.

| Per share, against the mid, at a 1¢ spread | Limit order (passive) | Market order (aggressive) |
| --- | --- | --- |
| Half the spread | +0.5¢ | −0.5¢ |
| Fee | +0.5¢ rebate | −1¢ commission |
| **Total** | **+1¢** | **−1.5¢** |

| Price gain on the round trip, before fees | −1¢ | 0¢ | +1¢ |
| --- | --- | --- | --- |
| Two passive fills (2 × ½¢ rebate) | 0¢ | +1¢ | +2¢ |
| Two market orders (2 × 1¢ commission) | −3¢ | −2¢ | −1¢ |

On paper the case rewards a passive market maker: even a round trip that loses 1¢ on price breaks even. A strategy that uses market orders pays about 3¢ per share per round trip (two commissions plus the spread), so it needs the price to move more than 3¢ in its favour.

## (a) What was your strategy going in?

I went in with a **momentum taker**: an algorithm that uses only market orders and follows the direction in which the price is being pushed.

**Why not market making.** I started with a passive market maker ([`algo2_market_making.py`](algo2_market_making.py)): 1,000 shares at the best bid and the best ask, reposted whenever the touch moved, with a price skew and a size throttle against inventory. In the practice simulations, the price of ALGO was pushed hard in one direction for long stretches, which looked like manipulation of the order flow. That is the worst case for a market maker. In a push up, buyers take the offers, so the quote that fills is my ask. I end up short just before the price rises further, and the next fill is again on the wrong side. The rebate of ½¢ per share is small next to an inventory loss of several cents per share per push.

**My answer.** If the price is being pushed, trade with the push instead of standing in front of it:

1. **Detect the push.** Fast EMA (4 ticks) minus slow EMA (15 ticks) of the mid, in cents.
2. **Follow it with market orders only.** When the signal is 1¢ or more, buy (or sell, if negative) with market orders. The target position is 2,500 shares per cent of signal, up to 10,000 shares. Market orders fill immediately, so the position follows the signal without waiting in the queue.
3. **Get out when it stops.** Close when the signal falls under 0.3¢ or flips sign, on a 5¢ loss per share, or after a $2,000 drop from the peak P&L. Flatten in the last 6 ticks.

The trade-off is deliberate: I give up the rebate and pay about 1.5¢ per share per trade, in exchange for being on the right side of the push.

## (b) How would you decide what the appropriate bid/ask spread should be? Should it be dynamic or static?

**Dynamic.** A static spread is either too wide or too narrow. The brief's example quotes LAST ± a fixed spread. If that spread is wider than the market, my quotes sit behind the touch and only fill when the price runs through them, which is the fill a market maker doesn't want. If it is narrower than the market in a fast market, I get run over.

**What a quote has to earn.** Each passive fill earns half my quoted spread plus the ½¢ rebate. It loses the adverse selection: how far the mid moves against me after I am filled. The tightest spread worth quoting is the one where half the spread plus the rebate still covers the adverse selection I expect. In a calm market that trades 1¢ wide, the touch is enough. When the price is being pushed, adverse selection is several cents per share, and no reasonable spread covers it.

**How I applied it.** My first version took its spread from the market (best bid and best ask), so it widened and tightened with the book, and stepped the side that adds to the position 1¢ back past 7,500 shares. In the practice cases this was not enough: the problem was the direction of the flow, not the width of the spread. The momentum taker answers the question at the extreme: when the expected adverse selection is larger than any spread I could quote, the right spread is "no quote", and I trade with the flow instead.

A market maker that keeps quoting should widen with the speed of the market (quote further from the mid when the mid has moved several cents in the last few seconds) and compute the touch from other traders' orders only, so it does not quote around its own prices.

## (c) What happened during the simulation? What was your size strategy? Please share your Python script with an explanation on how to use it.

### What happened

I lost money, because the market was manipulated.

### Size strategy

Size follows the strength of the push:

| Signal (fast EMA − slow EMA) | Target position |
| --- | --- |
| Under 0.3¢, or opposite sign to the position | Flat |
| 0.3¢ to 1¢, same sign as the position | Hold the current position |
| 1¢ | 2,500 shares |
| 2¢ | 5,000 shares |
| 4¢ or more | 10,000 shares (cap) |

- **Orders of at most 2,500 shares**, half the 5,000-share cap, 0.3 s apart. A large market order walks the book and pays more than the top-of-book price.
- **No adjustment under 1,000 shares** unless the target is flat. Every market order pays the commission, so small rebalancing trades are skipped.
- **Cap of 10,000 shares**, well under the 25,000 limit, so no order is ever rejected and there is no fine.

### The script ([`algo2_momentum.py`](algo2_momentum.py))

The script runs a loop every 50 ms through the RIT REST API:

1. **Read the case and the stock.** Tick, position, bid, ask and P&L for ALGO.
2. **Update the signal.** Once per case tick, update the two EMAs of the mid and their difference.
3. **Risk checks.** Stop-loss on a 5¢ loss per share (no new entries for 5 s). Drawdown breaker: if P&L falls $2,000 below its peak, flatten and pause for 20 s.
4. **Trade.** Compute the target position from the table above and send a market order toward it (at most 2,500 shares).
5. **End of case.** In the last 6 ticks, flatten with market orders.
6. **Monitor.** Print the tick, P&L, peak P&L, position and signal once per tick.

**How to use it**

1. `pip install requests`
2. Open the RIT client and load the ALGO2 case. Click the API icon in the status bar to check the port (default 8000) and copy your API key.
3. Paste the key into `API_KEY` at the top of `algo2_momentum.py`. Change `BASE_URL` if the port is not 8000.
4. Run `python algo2_momentum.py` in the Anaconda Prompt before the case starts. It waits for the case to start and resets itself if a new run of the case begins.
5. Stop it with Ctrl+C: it cancels all orders and keeps sending market orders for up to 10 s until the position is flat.

| Setting | Value | Role |
| --- | --- | --- |
| `EMA_FAST` / `EMA_SLOW` | 4 / 15 ticks | Signal = fast EMA − slow EMA of the mid |
| `ENTRY_GATE` / `EXIT_GATE` | 1¢ / 0.3¢ | Signal needed to trade / to stay in |
| `SHARES_PER_TREND_CENT` | 2,500 | Target position per cent of signal |
| `MAX_POSITION` | 10,000 shares | Cap on the position |
| `MAX_STEP` / `MIN_STEP` | 2,500 / 1,000 shares | Largest market order / smallest adjustment |
| `STOP_CENTS` | 5¢ per share | Stop-loss |
| `MAX_DRAWDOWN` / `DD_PAUSE` | $2,000 / 20 s | Drawdown breaker |
| `END_TICKS` | 6 | Flatten before the close |

## (d) What are the different ways you can alter your trading strategy, so that you keep submitting orders but attempt to balance your book?

The aim is to keep both quotes working, so I keep earning the spread and the rebate, while making the fill that reduces my inventory more likely than the fill that adds to it. These are the levers, the ones in my first version first:

1. **Move the price of the side that adds to the position (used).** Past 7,500 shares, that side steps 1¢ behind the touch, so it fills less often. A stronger version skews both quotes continuously with the position: when long, lower both the bid and the ask, so the ask fills more and the bid less.
2. **Cut the size of the side that adds to the position (used).** Past 15,000 shares, that side shrinks linearly to 0 at 20,000. Each fill then moves the position toward zero even when both sides trade.
3. **Hard stop below the limit (used).** At 20,000 shares only the reducing side is quoted. This rules out the 10¢ fine and rejected orders at 25,000.
4. **Improve the reducing side.** Post it 1¢ inside the spread when the spread is 2¢ or more, so it stands alone at the front of the queue and is the next order to fill.
5. **Unwind before the end.** From about tick 270, quote only the reducing side; in the last few ticks, close what is left with market orders.
6. **Step back from the side about to be run over.** When the book is heavily imbalanced, for example far more size on the bid than on the ask, the price is likely to rise, so pull the ask back.
7. **Pay to exit when a position goes against me.** If the price moves several cents against the average entry price, close part of the position with a market order and resume quoting.

Levers 1 to 3 are passive: they make the reducing fill more likely, but when the price is being pushed that fill does not come. That is what I saw in the practice cases, and why I moved to the momentum taker, which takes lever 7 to its end: it never waits for a passive fill.

## (e) Will market-making strategies typically work in a trending market?

**No, not without changes.** A market maker earns a small amount per round trip, about 2¢ per share here, and needs flow on both sides so that both legs of a pair fill. In a range, buyers and sellers alternate and inventory comes and goes. In a trend the flow is one-sided: in a rally, buyers take the offers, so the quote that fills is the ask, leaving the market maker short just before the price rises further. The inventory loss grows with the size of the move, while the spread income grows only with the number of fills.

My first version shows why. Every time the price ticks up, it cancels its ask and reposts it at the new best ask, where buyers keep filling it. The skew does not start until 7,500 shares and is only 1¢. In a steady push it keeps selling into the rally, 1,000 shares at a time, until the throttle stops it at 20,000 shares. A 20,000-share position against a 50¢ move loses $10,000, against at most a few hundred dollars of rebates.

In a trending or manipulated market, the profitable position is directional, the opposite of what a market maker ends up holding. That is the reasoning behind my momentum taker. Its weakness is the mirror image: in a calm range it pays 3¢ per round trip and earns nothing, and when a push reverses suddenly it is caught on the wrong side until the stop-loss or the exit gate closes it.

## (f) What was your P&L?

I lost money, because the market was manipulated.

## ALGO2e: what I built for the group case

For the group case (ALGO2e) I wrote two scripts, in [`algo2e/`](algo2e/). The group write-up covers the graded ALGO2e session and its P&L.

ALGO2e has three stocks with different fees per share:

| Ticker | Taking liquidity | Providing liquidity | What it means |
| --- | --- | --- | --- |
| CNR | Pay 0.27¢ | Earn 0.23¢ | Maker-taker: rebates help a market maker |
| RY | Earn 0.14¢ | Pay 0.20¢ | Inverted: makers pay, takers are paid |
| AC | Pay 0.15¢ | Earn 0.11¢ | Maker-taker, smaller rebate |

**[`algo2e_market_making.py`](algo2e/algo2e_market_making.py)** extends my ALGO2 market maker to three stocks and their fees:

- **Fees per ticker.** A quote is posted only if spread + 2 × rebate is at least 1.5¢ per share. On RY the rebate is negative, so a pair of passive fills costs 0.4¢, and RY needs a wider spread to be quoted. Its quote size is also smaller (300 shares against 500 on CNR and AC).
- **Fixes from ALGO2.** My own orders are removed from the book before computing the spread, the trend and the order sizes. The spread must also be at least 0.3 × the high-low range of the mid over the last 5 seconds, so the script does not quote a 1¢ spread on a stock that is moving 10¢.
- **Trend and imbalance filters.** No new buying while the mid has fallen 3¢ in 1.5 seconds or the book is ask-heavy, and the reverse for selling.
- **Exits.** If the price moves against the position, the script exits with a marketable limit order capped at the size of the top of the book, instead of a market order that walks a thin book.
- **Momentum mode.** When the mid moves 6¢ in 3 seconds and the book imbalance agrees, the script stops quoting that ticker and takes up to 3,000 shares in the direction of the trend. It exits on an 8¢ pullback from the best price or when the imbalance flips.
- **Circuit breakers.** A ticker that loses $2,500 from its peak P&L sits out for 20 seconds. Below −$15,000 in total, the script only unwinds. It flattens all positions in the last 12 ticks.

**[`algo2e_momentum.py`](algo2e/algo2e_momentum.py)** is the multi-stock version of my ALGO2 momentum taker. The order flow pushed prices into long trends in ALGO2e as well, so the same idea applies to all three stocks:

- **Signal.** Fast EMA (4 ticks) minus slow EMA (15 ticks) of the mid, in cents, per ticker.
- **Target position.** 1,500 shares per cent of signal, up to 6,000 shares per ticker (18,000 in total, under the 25,000 limit).
- **Orders.** Marketable limit orders, at most 3¢ through the touch and 2,000 shares each, instead of plain market orders, because the ALGO2e books are thinner. On RY a taker is paid 0.14¢ per share, so following the trend there also earns the fee.
- **Risk.** A stop-loss on a 5¢ loss per share, a pause of 20 seconds after a $2,000 drawdown from peak, and a flatten in the last 6 ticks. Ctrl+C cancels all orders and flattens the book.

To run either script, paste the API key into `API_KEY` at the top and run `python algo2e_market_making.py` or `python algo2e_momentum.py` before the case starts. In `algo2e_market_making.py`, set `MOMENTUM = False` for pure market making.

## Files in this folder

| File | What it is |
| --- | --- |
| `RIT_ALGO2_Writeup_Elouan.md` | This write-up |
| [`algo2_momentum.py`](algo2_momentum.py) | ALGO2: the momentum taker (market orders only) |
| [`algo2_market_making.py`](algo2_market_making.py) | ALGO2: my first, passive market-making version |
| [`algo2e/algo2e_market_making.py`](algo2e/algo2e_market_making.py) | ALGO2e: market maker for CNR, RY and AC with filters and a momentum mode |
| [`algo2e/algo2e_momentum.py`](algo2e/algo2e_momentum.py) | ALGO2e: momentum taker for CNR, RY and AC |
