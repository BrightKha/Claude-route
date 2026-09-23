# Gamma fixtures (real payloads)

Captured 2026-09-23 from `https://gamma-api.polymarket.com` (see `docs/research.md` §5).

| File | Content |
|---|---|
| `btc_5m_twap_events_2026-09-23.json` | 12 consecutive BTC 5m events (11 resolved + 1 resolving), full payloads, rule `btc_5m_twap60_v3`. |
| `btc_5m_spot_v1_event_2026-04-30.json` | One resolved event under the old spot rule (`btc_5m_spot_v1`), trimmed to relevant fields (values verbatim). |
| `btc_5m_twap60_market_open_1790127600.json` | `/markets?slug=` payload of an open, not-yet-started market (trimmed, values verbatim). |
| `btc_5m_zombie_event_2025-12-19.json` | Stale event returned by the series query (`closed=false`, `enableOrderBook=false`, end date in the past). Trimmed. |
