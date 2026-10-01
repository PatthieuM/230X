# RIT ALGO1 — rules and hard-won facts (encode for the next simulation)

Everything here is measured on the live RIT client (demo + heat, 10 Sep 2026), not assumed. The
booklet in `docs/RIT_case_brief_ALGO1.pdf` and the API in `docs/RIT_client_REST_API_1.0.3_swagger.yaml`
are the primary sources; this file is what they leave out and what only live contact revealed.

## Case (ALGO1 — Algorithmic Arbitrage)
- One stock **CRZY** on two venues: tickers **CRZY_M**, **CRZY_A**. Arbitrage the crossed book.
- **Positions are aggregated across the two tickers** (net). A filled pair (buy M / sell A) nets
  to zero: `/limits` gross AND net stay 0. So pairs are NOT position-limited; only an unhedged
  residual consumes the limit.
- **Max 10,000 shares per order** (`max_trade_size`). Cut larger quantities into 10k clips.
- **Position limit 25,000 gross and 25,000 net.** `is_enforce_trading_limits` = true: an order
  that would breach is **rejected** (fines were 0 here but do not rely on that).
- **No fees, no rebates** in this case (`trading_fee` = `limit_order_rebate` = 0). Still read them
  from the sheet at tick 0 — do not hard-code.
- Case is 300 ticks. Tick speed varies: **~1 tick/s in the heat (≈5 min), ~4 tick/s in the test.**
  The engine learns the rate from `/v1/case`; never assume.

## API / client behaviour (the part the doc omits)
- REST on `localhost:<port>`; key in the `X-API-Key` header. Each client has its **own port and
  key** (heat: account 1 = 24061, account 2 = 24062). 401 = wrong/empty key.
- **Rate limit is per client, from `api_orders_per_second`: 10/s in the test, 500/s in the heat.**
  The budget reads this at tick 0. GETs are NOT limited.
- **The client SERIALISES all connections** (4-lane probe: ~1.0–1.4x, never near 4x). Consequence,
  and it bit us: heavy order/cancel traffic on the ord/cxl lanes **blocks the poll lane**, so a
  cancel flood slows your own market view (heat: 43k–170k cancels → poll rate halved, 826→314/s).
  **Minimise resting-then-cancelled orders.** Prefer marketable/IOC legs that never rest.
- **Position field lags the acks by 30–200 ms** and is the aggregated case position. Fills must be
  reconciled with a lag envelope (a server reading inside the recent-position band is lag, not a
  fill) or the bot manufactures phantom fills and loops (the p2 blow-up).
- **A MARKET/marketable order's ack already carries the fill** (`quantity_filled`, `vwap`). Use the
  ack for aggressive fills; the position poll is attribution only.
- **An order registers on the server ~10 ms AFTER its ack.** A DELETE sooner returns 404
  "ORDER_NOT_FOUND". Defer cancels ~15 ms and retry.
- **Self-trade is NOT prevented.** A marketable order will hit your own resting order. The risk
  gate must drop/cancel the self-crossing leg.
- **HTTP 500 under load = the client is overloaded, not a venue halt.** Back off ~200 ms, do not
  trip the breaker (503 = real halt, e.g. the mock kill).
- Marketable LIMIT acks can say `status: OPEN` yet be fully filled — trust `quantity_filled >= quantity`, not the status string.

## Competition (the dominant P&L factor — measured heat vs test)
- ~50 students trade the same book. **Crosses are arbitraged away in milliseconds.** Books were
  crossed **1.8% of the time in the test, 0.3% in the heat**; edge per cross fell **~4 ticks → ~1.6**.
  The opportunity itself shrinks ~6x under the field. This is the game, not a bug.
- **Win the whole cross or don't play.** A cross has finite crossed quantity; 50 bots split it.
  If you fire two legs and wait between them, the cross is gone and you hold a one-sided residual
  whose mark-to-market dominates (and lies on) the P&L chart. **Fire both legs atomically, fast**;
  do not send the second leg conditional on the first (confirm-then-hedge cut completion 13.6%→1%).
- Completion rate (both legs filled) is THE metric: t1 (fast) 13.6% → confirm-then-hedge 1%.

## Two accounts
- `LastName_First_1` = code only (the HW02 record). `LastName_First_2` = anything.
- **Leaderboard keeps the best account.** The two share the same book, so two identical bots
  split the crosses and each does worse — run the bot on ONE account at full strength and make the
  other a decorrelated draw (different config, or manual), not a mirror.

## What actually happened (10 Sep heat)
Test made ~$20k (steady, 81 completed pairs, uncontested at 10/s). Heat made ~$0.8k. Causes, ranked:
1. Field arbitraged the edge away: crosses 6x rarer, 2.5x thinner. (Not fixable — it's the game.)
2. Our cancel flood clogged the serialised client → polls halved → missed more crosses. (Fixable:
   no resting legs.)
3. `confirm_then_hedge` default slowed execution → completion 13.6%→1%. (Fixable: revert to fast
   atomic legs; the position-lag envelope fix already removed the reason it was added.)

## Fixes (built and validated 10 Sep — `configs/fast.json`)
1. MARKET legs, fired concurrently and atomically, never rest → no cancel flood → the serialised
   client keeps polling ~880/s (heat a1 collapsed to 314 under 43k cancels). Validated on a
   latency+contested mock: fast 27/27 completion, 0 cancels, $12,074 vs careful 21/26, 8 cancels,
   $242. The mock only shows this WITH latency (delay_ms>0); zero-latency it hides the miss.
2. Real P&L from the server: poll `/v1/trader` `nlv`. The bot's fill accounting is corrupted by
   position lag and must never be trusted for P&L (heat: bot said $11k/$268k, blotter $837).
3. The cross signal already uses only acked resting orders + raw BBO, never the lagging position;
   with MARKET legs nothing rests, so netting is a clean no-op — no phantom crosses.
Still open: real MARKET legs can pay through if a cross vanishes mid-flight (mock fills optimistically);
watch nlv on the first live run and add a small min_edge_cross margin if slippage shows.
The field cap (~0.3% crossed, ~1.6 ticks) is not fixable — that is the competition.
