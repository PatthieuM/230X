# HW3 Part 2: RIT ALGO1 Write-up

Alex · Sep 30, 2026

## (a) Strategy going in

My plan was pure cross-venue arbitrage on CRZY: whenever the two exchanges crossed, I would buy on the cheap venue and sell on the rich one at the same moment. CRZY trades on a Main exchange (CRZY\_M) and an Alternate exchange (CRZY\_A), and RIT aggregates the position across both. So buying X shares on one and selling X on the other leaves me flat in CRZY and locks in the price gap.

There are two signals:

| Condition | Action | Profit per share |
| --- | --- | --- |
| CRZY\_M bid > CRZY\_A ask | Sell on Main, buy on Alternate | M bid − A ask |
| CRZY\_A bid > CRZY\_M ask | Sell on Alternate, buy on Main | A bid − M ask |

I chose to automate it because crosses in RIT close within a fraction of a second, too fast for manual trading. My guiding principles were:

- **Stay market-neutral.** The goal was never to bet on direction, only to capture the gap between venues.
- **Only trade crosses worth taking.** A cross had to exceed a minimum edge (MIN\_EDGE = $0.02/share) before the algo fired, so tiny crosses that execution noise could erase were skipped.
- **Only trade the profitable shares.** Size each trade to the liquidity resting at the crossed prices, so the order never walks the book into prices that are no longer crossed.
- **Respect the case limits.** 10,000 shares max per order, a 25,000-share gross/net position limit, and a 300-second session with no commissions.

## (b) What happened, size strategy, and the script

### What happened

The final version of the algo ran cleanly for the 300-second session, made 62 arbitrage trades and stayed close to flat. Getting there took several iterations, and most of what I learned came from the failures of earlier versions:

1. **Position double-counting.** RIT reports the same aggregated CRZY position on both the CRZY\_M and CRZY\_A rows. My first version summed the two rows, which doubled the exposure it thought it had. Its safety logic then over-traded, flipped the sign of the position and oscillated by about 35,000 shares per cycle, a loss of roughly $120k. The fix was to read the position from one row only.
2. **Rate limiting.** Polling too fast pushed the algo past RIT's limit of about 100 requests per second. The API then returned 429 errors, the orders meant to flatten a broken leg were rejected, and naked long positions built up to the 25,000-share limit. I slowed the loop to 0.10 s and made flatten orders retry.
3. **Market-flattening whipsaw.** When one leg filled and the other didn't, the algo flattened the imbalance with a market order. In a fast, wide-spread book, that meant buying high and selling low over and over, and it drained P&L. In the final version I turned market-flattening off (MARKET\_FLATTEN = False).
4. **Final configuration.** Both legs go out as market orders (MARKET\_ARB = True), so they always fill together and never leave a naked leg. Each leg is capped at the size resting at top of book to limit slippage, and MIN\_EDGE = $0.02.

The lesson: in this case, **execution and risk plumbing mattered far more than the signal.** Spotting a cross is trivial. Hitting both legs without leaving a directional position or getting rate-limited is the hard part.

### Size strategy

Each trade is sized by `order_size()` as the smallest of:

- **Liquidity at the cross:** min(bid size on the sell venue, ask size on the buy venue). Only shares resting at the crossed prices are profitable, and any extra shares sweep into uncrossed prices and lose money.
- **Per-order cap:** 10,000 shares.
- **Position headroom:** (25,000 − |current position|) / 2, because each round trip briefly adds up to twice the leg size before the legs net out.

Trades smaller than MIN\_LOT = 100 shares are skipped because they aren't worth the requests. In practice, trades were sized by top-of-book depth, not by the limits: I took whatever was actually resting at the cross and nothing more.

### The script ([`arbitrage.py`](arbitrage.py))

Each loop (every 0.10 s), the script:

1. Reads both order books from `/securities`.
2. Prints a heartbeat every 2 s with the best available cross vs. the required threshold, so you can see the market even when it isn't trading.
3. Checks both cross conditions against the threshold (2 × TX\_COST + MIN\_EDGE).
4. If one is met, sizes the trade and sends the sell leg and the buy leg.
5. Reads how much each leg filled and cancels any unfilled remainder, so no stale order can fill later.

**How to use it**

1. Install the dependency: `pip install requests`.
2. Open the RIT client, log in, and load the ALGO1 case.
3. Click the **API** icon on the RIT client's bottom status bar and paste that key into `API_KEY`. Check that the port in `BASE_URL` matches the client (the script uses `localhost:7238`).
4. Once the case is **ACTIVE**, run `python arbitrage.py`. The script exits right away if the case isn't running yet.
5. Watch the heartbeat lines and tune the settings below.

| Setting | Final value | What it does |
| --- | --- | --- |
| MIN\_EDGE | 0.02 | Minimum cross ($/share) to trade; raise it if you take losers, lower it if it rarely trades |
| MARKET\_ARB | True | Legs as MARKET (always fill) vs. marketable LIMIT (price-protected, can break) |
| MARKET\_FLATTEN | False | Whether to flatten broken legs and net exposure at market |
| CANCEL\_IF\_GONE | False | Let unfilled limit legs rest briefly and cancel them once the cross disappears |
| SLEEP | 0.10 s | Loop delay; lower is faster but risks the rate limit |
| MIN\_LOT | 100 | Smallest trade worth sending |
| TX\_COST | 0.00 | Per-share cost built into the threshold (zero in ALGO1) |

## (c) Market vs. limit orders

A market order trades off **price certainty for execution certainty**, and a limit order does the reverse. In a two-legged arb, that choice determines which risk you carry: slippage or a broken leg.

- **Market order:** executes right away against whatever is resting in the book, at whatever price it takes. You are guaranteed to fill but not guaranteed a price.
- **Limit order:** executes only at the stated price or better. If it is marketable (a buy priced at or above the ask, a sell at or below the bid), it fills right away against the liquidity resting at that level, and any remainder either rests or gets cancelled. You get price protection but no guarantee of a fill.

|  | Market orders | Limit orders (marketable, at the cross) |
| --- | --- | --- |
| Fill | Guaranteed | Only the size still resting at my price |
| Price | Not guaranteed | Never worse than the arb price |
| Main risk | **Slippage:** if the book moves or thins between my read and my order arriving, the order walks past the cross and the "arb" can lose money | **Legging risk:** one leg fills and the other doesn't, leaving me with a naked directional position in CRZY |
| Other risks | Worse in fast, thin books. Size must be capped to top-of-book depth | Closing the naked leg later costs about one spread (worse if done at market in a moving book). Resting remainders can fill later at stale prices unless cancelled |
| Cost | Pays the spread on both legs, which is fine as long as the cross is wider than any slippage | Pays the spread only on what fills. Could also earn the spread by resting passively, at the cost of adverse selection |

**What I saw in practice.** Limit legs protected my price, but they produced broken legs. My attempts to clean those up with market orders were the main source of losses in earlier versions. Switching both legs to market orders removed legging risk entirely, and I kept slippage small by capping each leg at top-of-book depth and requiring a minimum edge. With only one aggregated position and a hard position limit, a slightly worse fill was a much smaller risk than carrying unhedged inventory.

The trade-off doesn't go away. With market legs, MIN\_EDGE has to exceed the typical slippage or the strategy bleeds a few cents per trade. With limit legs, you need a disciplined, rate-limit-safe way to handle the leg that didn't fill.

## (d) P&L

The final run made **$50** over **62 trades**, with a **Sharpe ratio of 0.13**. That is about $0.81 per trade. The strategy was profitable and stayed hedged, but it captured very little of the edge that was available.

| Metric | Value |
| --- | --- |
| P&L | $50 |
| Trades | 62 |
| P&L per trade | ≈ $0.81 |
| Sharpe ratio | 0.13 |

Why the result is small:

- **Small size per trade.** Capping each leg at top-of-book depth kept me from walking the book, but it also meant many trades were only a few hundred shares. At a $0.02–0.05 edge, that is a few dollars at most.
- **Slippage ate the edge.** Market legs sometimes filled past the quoted cross because other traders (and the book) moved in the gap between my read and my order. A $0.02 threshold leaves almost no cushion for that.
- **Latency.** A 0.10 s polling loop (needed to stay under the rate limit) means the widest crosses were often gone before my orders arrived.
- **Low Sharpe.** Per-trade P&L swung between small gains and small losses, which is consistent with slippage roughly offsetting the edge on the thinner crosses.

What I would change next time:

1. Raise MIN\_EDGE to about $0.04–0.05, so I only take crosses wide enough to absorb typical slippage.
2. Cut the requests per loop (for example, stop calling `/case` every cycle) so the loop can run faster while staying under the rate limit.
3. Try limit legs again with a small, rate-limit-aware cleanup of broken legs. That keeps the price protection without the market-flattening whipsaw.
