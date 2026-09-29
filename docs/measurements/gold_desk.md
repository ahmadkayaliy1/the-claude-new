# Gold desk scorecard — XAUUSD (shadow, D-049)

Generated 2026-09-29T09:46:43.464Z by `tools/desk_report.py` from `C:\the_claude_new\data` (read-only). Window: all → 2026-09-29T09:46:43.464Z.

## Verdict for H32 (the evidence part only)

- `desk_ok` ideas resolved: **0** of the 30 needed; expectancy after the spread **– R** against the random-entry baseline -0.052 R + 0.15 R → **not met**.
- The other H32 conditions (equity ≥ `equity_for_min_lot` at the median shadow stop, or a ≤ 0.1-oz contract; production on the dedicated machine) are the owner's; nothing here trades.

## Shadow ideas

| | n | resolved | expectancy R (net) | TP1 first | median min | MFE R | MAE R |
|---|---|---|---|---|---|---|---|
| all shadow | 0 | 0 | – | – | – | – | – |
| desk_ok | 0 | 0 | – | – | – | – | – |
| not desk_ok | 0 | 0 | – | – | – | – | – |

Per idea (R net = the virtual R minus the spread at the gate over the stop):

| id | time | side | stop | shadow | desk_ok | failed (not waived) | outcome | R | R net | window |
|---|---|---|---|---|---|---|---|---|---|---|
| dfcef314 | 2026-09-28T17:07 | SELL | 7.20 | 0 | False | rr_after_costs | sl_first | -1.000 | -1.035 | outside_window |

## M1 — every stored XAUUSD BUY/SELL re-gated with the desk rules

1 ideas with a gate record; desk_ok 0; failed checks (not waived): {"rr_after_costs": 1}.

## M4 — calls per UTC day (stored cycles, last 14 days)

2026-09-27: 2 · 2026-09-28: 19 · 2026-09-29: 12

## Random-entry baseline

4992 entries (every 5-min close inside the desk windows, 2026-07-01 → 2026-09-29, 87907 1m bars), each a BUY and a SELL per stop size, target 2.0R, real per-bar spread (median 0.22), stop first on a shared bar, flat at 17:00 New York.

| stop | side | n | expectancy R | 2R first | median min | losses ≤ 15 min | by window |
|---|---|---|---|---|---|---|---|
| 2 | BUY | 4992 | -0.113 | 0.295 | 3 | 0.960 | london -0.163 (n 2496); new_york -0.064 (n 2496) |
| 2 | SELL | 4992 | -0.092 | 0.302 | 3 | 0.956 | london -0.024 (n 2496); new_york -0.161 (n 2496) |
| 2.9 | BUY | 4992 | -0.094 | 0.301 | 6 | 0.861 | london -0.160 (n 2496); new_york -0.028 (n 2496) |
| 2.9 | SELL | 4992 | -0.039 | 0.320 | 6 | 0.860 | london +0.029 (n 2496); new_york -0.107 (n 2496) |
| 5 | BUY | 4992 | -0.071 | 0.306 | 16 | 0.573 | london -0.161 (n 2496); new_york +0.020 (n 2496) |
| 5 | SELL | 4992 | +0.027 | 0.342 | 16 | 0.578 | london +0.124 (n 2496); new_york -0.070 (n 2496) |
| 9 | BUY | 4992 | -0.081 | 0.295 | 51 | 0.235 | london -0.106 (n 2496); new_york -0.057 (n 2496) |
| 9 | SELL | 4992 | +0.050 | 0.342 | 54 | 0.258 | london +0.143 (n 2496); new_york -0.043 (n 2496) |

Break-even at a 2R target is a 2R-first share of 0.333. A baseline is not an edge: it is what a random entry at the same stop, hours and costs earned.

Sample size: a handful of ideas proves nothing; the verdict needs 30 resolved desk_ok ideas.
