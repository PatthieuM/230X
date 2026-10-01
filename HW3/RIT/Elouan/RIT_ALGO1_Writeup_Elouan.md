# HW3 Part 2: RIT ALGO1 Write-up

Elouan Bahri · Sep 30, 2026

Script: [`algo1_arbitrage.py`](algo1_arbitrage.py)

## (a) Strategy going in

ALGO1 lists the same stock, CRZY, on two venues: a Main exchange (CRZY\_M) and an Alternate exchange (CRZY\_A). RIT nets the position across both. My strategy was pure cross-venue arbitrage: whenever one venue's best bid is above the other venue's best ask, buy on the cheap venue and sell on the rich one at the same time. The two legs offset, so I stay flat in CRZY and lock in the gap.

| Condition | Action | Edge per share |
| --- | --- | --- |
| CRZY\_M bid > CRZY\_A ask | Buy on A, sell on M | M bid − A ask − 2 × cost |
| CRZY\_A bid > CRZY\_M ask | Buy on M, sell on A | A bid − M ask − 2 × cost |

Crosses close within a fraction of a second, which is too fast to trade by hand, so I automated it. My guiding principles were:

- **Market-neutral.** Never take a view on direction. Only capture the price gap between venues.
- **Fill both legs or neither.** An unhedged leg is a directional bet, so both legs are sent as MARKET orders at the same time.
- **Respect the case limits.** 10,000 shares max per order, 25,000 shares max gross and net position, 300-second session, no commissions.

## (b) What happened, size strategy, and the script

### What happened

I ran into technical bugs during the session and could not get the script to execute against the live case, so the bot did not trade in this simulation. I have more insight from the second RIT simulation (ALGO2, algorithmic market making), where I was able to run my algorithm.

### Size strategy

Each trade's size is the smallest of three caps:

1. **Liquidity at the cross:** `min(bid size on the sell venue, ask size on the buy venue)`. Only the shares resting at the crossed prices are profitable. Anything larger walks the book into prices that are no longer crossed and loses money.
2. **Per-order limit:** 10,000 shares (`ORDER_LIMIT`).
3. **Position limits:** `max_tradable_quantity()` simulates the trade and lowers the size until neither the gross nor the net position after the trade exceeds 25,000 shares.

Because both legs have the same size, a fully filled trade leaves the net position unchanged.

### How the script works

The main loop, `main()`, polls `/case`. While the case is `ACTIVE`, each tick does the following:

1. **Read both books in parallel.** `check_and_trade()` fetches the top of book for CRZY\_M and CRZY\_A concurrently through a `ThreadPoolExecutor`, so the two quotes are as close in time as possible.
2. **Compute both edges.** If there is no cross, the tick ends right away. Positions are only fetched when there is a cross, which saves one API call per tick.
3. **Size the trade** as described above.
4. **Send both legs at once.** `submit_both_legs()` sends the BUY and the SELL MARKET orders concurrently instead of one after the other, which shortens the window where only one leg has filled.
5. **Monitor.** The script prints P&L (realized + unrealized across both tickers) once per second. It sleeps `POLL_SLEEP = 0.05` s between ticks and backs off 0.5 s whenever the API returns HTTP 429 (rate limit).

The script stops on its own when the case status becomes `STOPPED`.

### How to use it

1. `pip install requests`
2. Open the RIT Client and load the ALGO1 case. Click the API icon in the bottom status bar to check the port (default 8000) and copy your API key.
3. Paste the key into `API_KEY` at the top of `algo1_arbitrage.py`. Change `BASE_URL` if the port is not 8000.
4. Run `python algo1_arbitrage.py` before or right after the case starts. The script waits for `ACTIVE` and exits on `STOPPED`.
5. Optional: set `TRADING_COST` to a per-share cost to see how fees raise the minimum profitable cross. The edge is reduced by `2 × TRADING_COST`, one cost per leg.

## (c) MARKET vs LIMIT orders

**MARKET orders** execute immediately against the resting book.

- *Advantage:* fills are guaranteed (given liquidity), so both legs of the arbitrage fill and no unhedged position is left over.
- *Risk: price/slippage.* The price is not guaranteed. If the book moves between my quote read and my order's arrival, or if the order is larger than the top level, it fills at worse prices and the "arbitrage" can lose money. MARKET orders also always pay the spread.

**LIMIT orders** execute only at my price or better.

- *Advantage:* price protection. I never pay more than the crossed price, so any fill keeps its edge, and resting limit orders can earn the spread instead of paying it.
- *Risk: execution/leg risk.* A fill is not guaranteed. In an arbitrage, one leg can fill while the other doesn't because the cross closed in the meantime. That leaves a naked directional position that must be unwound, often with a MARKET order at a loss. Unfilled resting orders can also be picked off when the price moves against them.

In short, MARKET orders trade **price risk** for **execution certainty**, and LIMIT orders trade **execution risk** for **price certainty**. Crosses in ALGO1 are short-lived, and a broken leg is the most costly failure, so I chose MARKET orders for both legs. I limited their price risk by sizing each trade to the liquidity at the top of book.

## (d) P&L

Because of the technical bugs described in (b), the script did not execute during the session, so I have no P&L to report for ALGO1.
