# HW3 Part 2: RIT ALGO1 Write-up

Alex · Sep 30, 2026

## (a) Strategy going in

My strategy was basic arbitrage between the two CRZY exchanges: buy where it's cheap, sell where it's expensive, and pocket the difference.

Every loop, the algo checks the spread between the two venues: the best bid on one exchange minus the best ask on the other. If that spread is bigger than my threshold X (MIN\_EDGE = $0.02/share), it buys on the low exchange and sells on the high one at the same moment. Because the position is aggregated across the two venues, the two legs cancel out. That leaves me flat with the spread locked in as profit.

If the spread is below X, I do nothing. A tiny cross isn't worth it, because slippage or a fill that doesn't line up can erase it. When the spread is big enough, I trade only the size resting at the best prices, since anything bigger would eat into prices that are no longer profitable.

To execute, my original plan was to send both legs as limit orders priced at the crossed prices, so I could never fill worse than the arb. The risk is a broken leg: one side fills and the other doesn't, leaving me with a directional position. My plan for that was to flatten the leftover shares immediately with a market order to get back to zero.

## (b) What happened, size strategy, and the script

The final version of the algo ran cleanly for the 300-second session, made 62 arbitrage trades and stayed close to flat. Getting there took several iterations of testing before the live session to fix three problems. All three happened in testing, not in the graded run.

First, RIT shows the same aggregate position on both tickers. My first version summed the two, which doubled my exposure and caused a ~$120k loss in testing, so I switched to reading a single row. Second, polling too fast triggered rate-limit errors, so my hedge orders were rejected and naked longs built up. I fixed this by slowing the loop to 0.10 s. Third, flattening unmatched legs at market kept buying high and selling low. The final version sends both legs as market orders sized to top of book, so they always fill together.

### Size strategy

Each trade is the smaller of the size at the best bid on the sell venue and the size at the best ask on the buy venue. It is also capped at 10,000 shares and at half the remaining room under the 25,000-share limit. Trades under 100 shares are skipped. In practice, top-of-book depth set the size.

### The script ([`arbitrage.py`](arbitrage.py), attached)

Every 0.10 s the script reads both books, checks for a cross above MIN\_EDGE, sends both legs, and cancels any unfilled remainder. To run it, paste your RIT API key into `API_KEY` and run `python arbitrage.py` once the case is active.

## (c) Market vs. limit orders

A market order guarantees a fill but not a price, while a limit order guarantees a price but not a fill. The main risk with market orders is slippage. If the book moves before my order arrives, I fill past the cross and can lose money on the "arb." The main risk with limit orders is a broken leg: one side fills and the other doesn't. That leaves me with a naked position that I usually have to close at a loss. In this case broken legs hurt me more than slippage, so I ended up using market orders sized to top of book.

## (d) P&L

I made **$50** over **62 trades** (about $0.81 per trade), with a **Sharpe ratio of 0.13**. The strategy was profitable and stayed hedged, but the profit was small for two reasons. Trades were sized to thin top-of-book depth, and slippage on the market orders ate most of the $0.02 edge. Next time I would raise MIN\_EDGE to about $0.04–0.05 so each trade has room to absorb slippage.
