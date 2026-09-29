<!-- prompt: desks/xauusd_fields · version 1 -->
### Gold fields
- `market.gold_clock`: `asia_range` 00-07 UTC today (= `levels.asia_high`/`asia_low` for gold; `width_x_median` vs 30 days); `london_swept` {side, back_inside} = London 08-12 took an Asia extreme; `london_asia_sweep_days` {"30"|"90": [days, swept %, swept and back inside %]} = a measured base rate; `next` = [event, time, minutes away]; `desk_window` = open or closed:<reason>.
- `levels.round` {"10"|"50": [below, ATR distance, above, ATR distance]}.
- `market.news`: `blackout` none or [tier, release, time, until] (no entry while on); `next` = [time, tier, release, minutes away]; `unavailable` = stale calendar (no blackout is known).
- `market.cross`: `EURUSD` = the dollar inverted; `XAGUSD` = silver, `confirms_xau_extreme` high/low or 0; `votes` = what each 1h trend implies for gold (information only; |corr| < 0.3 casts none).
- `history[]` of a shadow idea: `shadow` 1, `desk_ok` 1/0, `gate_reason` = the checks it failed (the account-size checks are waived).
