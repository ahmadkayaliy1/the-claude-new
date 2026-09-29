# Phase 5 checkpoint B — the one live call (2026-09-29)

## Setup

`engine --once --pairs BTCUSDT` from the worktree (`feat/phase5-goes-deeper` at `42bcf7e`, payload v4 / view v3 /
legend v5 / persona v3), started 12:01:23 UTC — right after the 12:00 UTC five-hour reset — on a scratch data root:
- `hot` and `cold` as `mklink /J` junctions to production (removed afterwards with `rmdir`; production untouched);
- a SQLite-backup copy of production's `data\instances\BTCUSDT\app.db` taken seconds before the run (from a
  `mode=ro` connection), so the executor's account report was fresh ("live demo account, reported by the executor 1 s
  ago");
- its own empty `shared\ai_usage.db` and a copy of `cli_capabilities.json` (the stream-json shape is not re-probed);
- `TRADINGSYSTEM_CONFIG` = the worktree's `config.yaml` merged with production's `config.local.yaml` (no secrets) and
  absolute scratch paths; `EXECUTION_MODE=demo`, `EXECUTION_TRIGGER=auto`, `TS_INSTANCE=BTCUSDT`,
  `TS_NOTIFY_DISABLE=1`, `TS_NEWS_NO_FETCH=1`; the calling session's `CLAUDE_CODE_*` variables removed;
- stdout / stderr redirected to files (the Phase 3 lesson). A dry run beforehand (one payload build on the same root,
  no call) checked the setup.

## Result

| measure | acceptance (§3.9.1) | result |
|---|---|---|
| status | valid, first attempt | **valid**, one ledger row, `num_turns` 1, no repair |
| images | 6 | **6** (1w, 1d, 4h, 1h, 15m, 5m; estimate 2 304 tokens); the renderer's first set +28.5 MB RSS |
| input tokens | ≤ 26.7 k | **25 688** (cache creation 25 686, the first call of the new prompts; fresh 2) |
| vs the day's production median | ≤ +1.5 k | **+398** (production BTC decision calls 2026-09-29, n 20: median 25 290) |
| output tokens | — | 3 345 |
| latency | — | **47.7 s** (60.8 s wall incl. the sign-in check and the charts) |
| every new block in the stored payload | present | **yes**: `payload_version` 4; `orderflow.depth` real (±0.1/0.25/0.5 %, 23 s old); `levels.pwh/pwl/pmh/pml/month_open/year_open`; `timeframes.15m/5m.forming`; `orderflow.daily_profiles` real (5 days, $10 bins); `market.session_stats`; `market.cross` real (ETHUSDT corr 0.94, beta 0.67); `account.min_position_risk.fits_now` 1 (8.9× at the minimum lot, `equity_for_min_lot` 84); `min_stop_set_by: system_spread_rule`; capability `cross_asset` real; the stored payload keeps `1m`, the view drops it |
| `snapshot_build_ms` | < 3 s | **1 659 ms** cold, **418 / 410 ms** warm — the same code on the same scratch root right after the call (`engine --once` does not record its own builds) |
| decision | — | NO_TRADE, confidence 40, `next_review` 30 min with a 15m close below 84 070 / a 1h close above 84 999 |

The answer used the new blocks as intended: "Forming 15m bar still open, unconfirmed", the OI 30-day rank ("rank
2/30d, low conviction"), the depth / order flow only as short-horizon context ("not used as sole trigger").

## The budget behind it (measured before the call, read-only)

- **Model view, every 5-min screen of the stored day 2026-09-28 (BTCUSDT, 289 screens):** median 19 059 chars with the
  production code (payload v3 / view v2) → **17 755** with checkpoint B (−1 304; p95 19 328 → 18 014). The 1m view
  (≈ 2.1 k chars) pays for depth, the period levels, the forming bars, the profiles, the session statistics and cross.
- **System prompt:** the shared legend +2.1 k chars (v4 → v5); the gold-only lines (gold clock, round numbers, news,
  gold cross and votes) live in the XAU appendix `desks/xau.md`, so BTC/ETH do not pay for them. Net for a BTC call
  ≈ +0.2 k tokens — the measured +398 against the day's median agrees.
- **XAU (dry build + render, no call):** the gold-specific input ≈ 2.7–3.0 k chars (the appendix 1.8 k + gold clock,
  round numbers, news and cross in the view 0.6–1.2 k) ≈ 750–800 tokens against the post-B7 baseline; the brief is in
  the XAU system prompt (`desks/xau` version recorded), BTC/ETH system prompts are byte-identical without an appendix.
- **XAU calls (M4, `tools/replay_triggers.py XAUUSD --hours 24`, Monday 2026-09-28):** 29 setup/idle calls a day
  without the desk windows → **9** with them (59 fired screens held back as `outside_window`); reviews and events come
  on top. The news blackout was not replayed (no stored calendar covers that day).
