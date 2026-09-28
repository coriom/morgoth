# Backtest historical-source audit (2026-08-19)

**Purpose.** The grounding rewrite's verdict was PARTIAL: phantom rate dropped
but verifiable-share did not rise, because 46 % of non-directional theses are
about REAL live metrics the scorer can't reach historically. This audit finds
what free historical sources exist for those dark metrics, so a future slice
can wire scorer-side fetches (no new cycle tools, no gates — the backtest is
offline analysis).

## Demand (current backtest, n=361 theses, 150 dark rows)

| Metric                | rows | reachable today |
|-----------------------|-----:|-----------------|
| market capitalization |   35 | NO              |
| dominance             |   28 | NO              |
| ETH hash rate         |   19 | UNFIXABLE (PoS) |
| gas price             |   19 | NO              |
| funding rate          |   14 | NO              |
| market volume         |   12 | NO              |
| network congestion    |    9 | NO (gas proxy)  |
| on-chain metrics      |    5 | NO (too vague)  |
| mining profitability  |    5 | NO (composite)  |

**Currently reachable:** btc_hashrate + btc_difficulty (mempool.space 3m) +
market_sentiment (alternative.me F&G 90d). Everything else dark.

## Historical-source verdict (probed live 2026-08-19)

| Metric        | Source                                                          | Auth      | Depth      | Verdict          |
|---------------|-----------------------------------------------------------------|-----------|------------|------------------|
| market cap    | CoinGecko `/coins/{id}/market_chart` `market_caps` array        | keyless   | 90d hourly | REACHABLE-KEYLESS ✓ |
| volume        | same endpoint, `total_volumes` array                            | keyless   | 90d hourly | REACHABLE-KEYLESS ✓ (already fetched in directional backtest, just discarded) |
| funding rate  | Binance `/fapi/v1/fundingRate?symbol=BTCUSDT` (or Bybit v5)     | keyless   | many months, 8h cadence | REACHABLE-KEYLESS ✓ |
| gas price     | Owlracle `/v4/eth/history?candles=90&timeframe=1440`            | keyless   | 90d daily  | REACHABLE-KEYLESS ✓ |
| dominance     | CoinGecko `/global/market_cap_chart` → HTTP 401 PRO only        | paid      | —          | PAID-ONLY        |
| global mkt cap| CoinGecko `/global/market_cap_chart` (same as dominance)        | paid      | —          | PAID-ONLY        |
| congestion    | proxied by gas price (Owlracle)                                 | keyless   | 90d        | REACHABLE-KEYLESS (proxy) |
| on-chain metrics | too vague to map to one metric                               | —         | —          | UNSCORABLE       |
| mining profit | composite of hashrate × price − cost                            | —         | —          | UNSCORABLE (compose from hashrate + price if needed) |
| ETH hash rate | metric doesn't exist post-merge                                 | —         | —          | UNFIXABLE        |

## Ranked plan (by rows-unlocked × ease)

1. **CoinGecko market_chart already returns market_caps + total_volumes.**
   The directional backtest already fetches this endpoint for BTC + ETH and
   only reads the `prices` array. Reading the two sibling arrays adds ZERO
   new HTTP calls. **Unlocks 35 (mkt cap) + 12 (volume) = 47 rows.** Effort
   ≈ 20 lines in `analysis/thesis_backtest_descriptive.py` — new subject
   mappings + two scoring functions that reuse the existing nearest_value
   lookup.
2. **Owlracle gas history.** One new keyless GET (`/v4/eth/history`, 90
   daily candles). **Unlocks 19 (gas) + up to 9 (congestion as proxy) ≈ 28
   rows.** Effort ≈ 30 lines — new fetcher + score_gas().
3. **Binance funding rate history.** One new keyless GET. **Unlocks 14
   rows.** Effort ≈ 30 lines — new fetcher + score_funding().
4. **Dominance / global market cap.** PAID-ONLY on CoinGecko (28+ rows).
   Skip unless a paid tier is provisioned. Alternative: derive dominance
   from BTC market cap / (BTC + ETH + top-N) but that's a partial proxy
   and needs a design decision separate from this audit.

If (1)+(2)+(3) land: unreachable drops from 150 → ~63 (mostly dominance +
19 ETH-phantom + subjective). **Verifiable share on the full corpus
would rise from ~22 % to ~46 %** — enough to make future backtests
statistically confident on the confidence-inversion and hit-rate signals.

## Scorer-side fetch, NOT a new cycle tool

The backtest is offline analysis. It scores theses against PAST values;
it doesn't give Morgoth a new live capability. Therefore:
- No new `tools/data_feeds/*.py` module.
- No 14-gate walk, no shadow verifier, no proposal flow.
- Just a fetch inside `analysis/thesis_backtest_descriptive.py` + a
  small scorer function per metric. Same pattern as the existing
  hashrate/difficulty/F&G scorers.

If Morgoth later needs live historical access in the cycle (e.g. to check
"is BTC dominance rising vs 24h ago"), THAT would require a gated tool —
but the scoring backtest doesn't.

---
Baseline commit `9a8ef40`. Corpus 361 (post-grounding). No code changes
in this audit.
