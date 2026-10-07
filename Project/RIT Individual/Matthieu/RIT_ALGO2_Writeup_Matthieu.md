# 2.1 ALGO 2 – Individual

**Matthieu Pascal**

## a. What was your strategy going in?

A passive market-making algorithm: keep a bid and an ask resting at the top of the book at all times and earn the spread plus the 0.5¢ passive rebate on each side.

My working assumption was that most competitors would run similar AI-generated bots that cancel and resubmit their pair of orders on every loop. I therefore built the algorithm around three ideas:

- **Queue priority.** An order that is still at a good price is never cancelled, so it stays ahead of the bots that keep re-submitting.
- **No penny wars.** Improve the price by one tick only when the spread of the other participants is wide enough; otherwise join the best price.
- **Inventory skew.** Shift both quotes against the position and use smaller sizes on the side that adds inventory.

## b. How would you decide what the appropriate bid/ask spread should be? Dynamic or static?

Dynamic.

The floor is set by the fee structure. With a 0.5¢ rebate per side, even a zero-spread round trip earns 1¢ per share, so joining a 1¢ market is still profitable. My algorithm improved by one tick only when the spread of the other participants was at least 3¢, and joined the best bid and ask otherwise.

In hindsight the spread should also widen with volatility and with the strength of the trend, to compensate for adverse selection. My algorithm adjusted the *position* of its quotes (skew) but never widened them, which was its main weakness in the graded run.

## c. What happened during the simulation? What was your size strategy? Script.

**Practice sessions.** The market was flat (the price moved 3 to 6 cents over a whole case) with a 1¢ spread. Fills arrived in blocks equal to my full size at the best price, so size at the top of the book was the binding constraint. I scaled up from 1,500 shares per side to three 5,000-share orders at the best price, plus 4,000 and 3,000 shares one and two ticks behind. Position plus open orders was capped at 22,000 shares to stay under the 25,000 limit. This approach ranked 4th in the class in practice (about $4,400 per run, against $6,800 for the best).

**Graded run.** The price trended from 20.00 to above 21.20 and then back down. The algorithm had been tuned on flat markets and was not prepared for a move that directional:

- Buyers repeatedly lifted my offers, leaving me short up to 16,000 shares in a rising market.
- The inventory skew moved my quotes but did not close the position, and there was no price-based stop-loss.
- Restarting the script did not flatten the existing position, so each restart inherited the losing inventory.

Over the run I traded about 249,000 shares each way, buying at an average of 20.58 and selling at an average of 20.54.

**Script.** `algo2_market_maker.py` (in this folder). To use it:

1. Open the RIT client, log in, and note the API port and API key shown in its API panel.
2. Before the case starts, run `python algo2_market_maker.py --key YOUR_API_KEY --port YOUR_PORT`.
3. The script waits for the case to start, quotes automatically, prints one status line per second (position, best bid/ask, trend, P&L), closes the position in the last seconds and cancels its orders when it stops. Run a single instance at a time.

It needs Python 3 and the standard library only. The header of the file documents the strategy and every parameter.

## d. Ways to keep submitting orders while balancing the book

1. **Price skew.** Shift both quotes against the position: lower when long, higher when short.
2. **Size skew.** Quote less on the side that adds inventory and more on the side that reduces it.
3. **One-sided quoting.** Beyond an inventory threshold, quote only the side that reduces the position.
4. **Signal-based lean.** Shift quotes with a short-term trend or order-book-imbalance signal so that fewer orders are filled against the move.
5. **Hard limits.** Cap position plus open orders, and cross the spread to cut inventory when a position or loss threshold is reached.

My algorithm used 1, 2 and a position-based version of 5. What it lacked was a loss-based trigger for 5.

## e. Will market-making strategies typically work in a trending market?

Generally not. In a trend the market maker is filled almost only on the side facing the move and accumulates inventory against it. The few cents earned from spread and rebates are small compared with the mark-to-market loss on that inventory.

My result illustrates this: commissions ($4,954) and rebates ($4,501) roughly cancelled, and essentially the whole loss came from buying higher than I sold.

## f. What was your P&L?

**−$11,696.11**

| Component | Amount |
|---|---|
| Trading (248,718 shares bought at 20.5812, sold at 20.5360) | about −$11,243 |
| Commissions | −$4,954.36 |
| Rebates | +$4,501.49 |
| **Total (NLV)** | **−$11,696.11** |

Final position flat, no position-limit fines, 138 trades.
