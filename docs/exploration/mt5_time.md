# MetaTrader 5 (Windsor) — server time model (P1.7)

_Generated 2026-09-25T08:29:58.047Z by `research/probes/probe_mt5_time.py` from live/real data. Re-run to refresh._


## Live offset (server-scale tick time − true UTC)

| symbol | last tick (server scale) | true UTC now | diff h | rounded |
|---|---|---|---|---|
| BTCUSD@ | 2026-09-25T11:30:01.122Z | 2026-09-25T08:30:01.201Z | 3 | 3 |
| ETHUSD@ | 2026-09-25T11:30:01.122Z | 2026-09-25T08:30:01.201Z | 3 | 3 |
| XAUUSD@ | 2026-09-25T11:30:00.911Z | 2026-09-25T08:30:01.201Z | 2.9999 | 3 |

Local clock correction applied: Binance − local = 230 ms. Stale ticks (closed market) show a larger negative residual; the crypto CFDs trade continuously and give the clean reading.


## History available

BTCUSD@ H1 bars: 62199 from 2015-11-15T22:00:00.000Z (server scale) to 2026-09-25T11:00:00.000Z; Binance BTCUSDT 1h bars fetched: 79701. Terminal maxbars limits this depth (H1 → P1.6).


## Historical offset regimes (best-correlated shift per ISO week)

| from week | to week | weeks | server = UTC+k | min best corr |
|---|---|---|---|---|
| 2019-W01 | 2019-W10 | 10 | 0 | 0.739 |
| 2019-W11 | 2019-W44 | 34 | 1 | 0.854 |
| 2019-W45 | 2020-W05 | 13 | 0 | 0.971 |
| 2020-W06 | 2020-W13 | 8 | 2 | 0.975 |
| 2020-W14 | 2020-W43 | 30 | 3 | 0.967 |
| 2020-W44 | 2021-W12 | 22 | 2 | 0.957 |
| 2021-W13 | 2021-W43 | 31 | 3 | 0.896 |
| 2021-W44 | 2022-W12 | 21 | 2 | 0.84 |
| 2022-W13 | 2022-W43 | 31 | 3 | 0.938 |
| 2022-W44 | 2023-W12 | 21 | 2 | 0.917 |
| 2023-W13 | 2023-W43 | 31 | 3 | 0.977 |
| 2023-W44 | 2024-W13 | 22 | 2 | 0.979 |
| 2024-W14 | 2024-W43 | 30 | 3 | 0.968 |
| 2024-W44 | 2025-W13 | 22 | 2 | 0.903 |
| 2025-W14 | 2025-W43 | 30 | 3 | 0.967 |
| 2025-W44 | 2026-W13 | 22 | 2 | 0.973 |
| 2026-W14 | 2026-W39 | 26 | 3 | 0.997 |

Weeks analysed: 404; weeks with weak identification (corr < 0.8 or margin < 0.2): 1.


## Check against rule `UTC+2, UTC+3 while New York observes DST`

Weeks not matching the rule: 81 of 404.

| week | measured k | rule k |
|---|---|---|
| 2019-W01 | 0 | 2 |
| 2019-W02 | 0 | 2 |
| 2019-W03 | 0 | 2 |
| 2019-W04 | 0 | 2 |
| 2019-W05 | 0 | 2 |
| 2019-W06 | 0 | 2 |
| 2019-W07 | 0 | 2 |
| 2019-W08 | 0 | 2 |
| 2019-W09 | 0 | 2 |
| 2019-W10 | 0 | 2 |
| 2019-W11 | 1 | 3 |
| 2019-W12 | 1 | 3 |
| 2019-W13 | 1 | 3 |
| 2019-W14 | 1 | 3 |
| 2019-W15 | 1 | 3 |
| 2019-W16 | 1 | 3 |
| 2019-W17 | 1 | 3 |
| 2019-W18 | 1 | 3 |
| 2019-W19 | 1 | 3 |
| 2019-W20 | 1 | 3 |
| 2019-W21 | 1 | 3 |
| 2019-W22 | 1 | 3 |
| 2019-W23 | 1 | 3 |
| 2019-W24 | 1 | 3 |
| 2019-W25 | 1 | 3 |
| 2019-W26 | 1 | 3 |
| 2019-W27 | 1 | 3 |
| 2019-W28 | 1 | 3 |
| 2019-W29 | 1 | 3 |
| 2019-W30 | 1 | 3 |
| 2019-W31 | 1 | 3 |
| 2019-W32 | 1 | 3 |
| 2019-W33 | 1 | 3 |
| 2019-W34 | 1 | 3 |
| 2019-W35 | 1 | 3 |
| 2019-W36 | 1 | 3 |
| 2019-W37 | 1 | 3 |
| 2019-W38 | 1 | 3 |
| 2019-W39 | 1 | 3 |
| 2019-W40 | 1 | 3 |
| 2019-W41 | 1 | 3 |
| 2019-W42 | 1 | 3 |
| 2019-W43 | 1 | 3 |
| 2019-W44 | 1 | 3 |
| 2019-W45 | 0 | 2 |
| 2019-W46 | 0 | 2 |
| 2019-W47 | 0 | 2 |
| 2019-W48 | 0 | 2 |
| 2019-W49 | 0 | 2 |
| 2019-W50 | 0 | 2 |
| 2019-W51 | 0 | 2 |
| 2019-W52 | 0 | 2 |
| 2020-W01 | 0 | 2 |
| 2020-W02 | 0 | 2 |
| 2020-W03 | 0 | 2 |
| 2020-W04 | 0 | 2 |
| 2020-W05 | 0 | 2 |
| 2020-W11 | 2 | 3 |
| 2020-W12 | 2 | 3 |
| 2020-W13 | 2 | 3 |


## XAUUSD@ weekly close (last H1 bar before weekend)

| last bar open (server) | k by rule | session end (UTC) | session end (New York) |
|---|---|---|---|
| Fri 2026-07-31 23:00 | 3 | Fri 21:00 UTC | Fri 17:00 NY |
| Fri 2026-08-07 23:00 | 3 | Fri 21:00 UTC | Fri 17:00 NY |
| Fri 2026-08-14 23:00 | 3 | Fri 21:00 UTC | Fri 17:00 NY |
| Fri 2026-08-21 23:00 | 3 | Fri 21:00 UTC | Fri 17:00 NY |
| Fri 2026-08-28 23:00 | 3 | Fri 21:00 UTC | Fri 17:00 NY |
| Fri 2026-09-04 23:00 | 3 | Fri 21:00 UTC | Fri 17:00 NY |
| Fri 2026-09-11 23:00 | 3 | Fri 21:00 UTC | Fri 17:00 NY |
| Fri 2026-09-18 23:00 | 3 | Fri 21:00 UTC | Fri 17:00 NY |

Expected: session end = Friday 17:00 New York (standard 'NY-close' broker convention).


## XAUUSD@-derived offset regimes (weekly close = 17:00 New York assumption)

XAUUSD@ H1 history: 44885 bars from 2019-02-24T23:00:00.000Z (server scale).

| from week | to week | weeks | server = UTC+k |
|---|---|---|---|
| 2019-W09 | 2019-W10 | 2 | 0 |
| 2019-W11 | 2019-W44 | 33 | 1 |
| 2019-W45 | 2020-W05 | 13 | 0 |
| 2020-W06 | 2020-W13 | 8 | 2 |
| 2020-W14 | 2020-W26 | 13 | 3 |
| 2020-W27 | 2020-W27 | 1 | 0 |
| 2020-W28 | 2020-W43 | 16 | 3 |
| 2020-W44 | 2020-W47 | 4 | 2 |
| 2020-W48 | 2020-W48 | 1 | -1 |
| 2020-W49 | 2021-W12 | 16 | 2 |
| 2021-W13 | 2021-W43 | 31 | 3 |
| 2021-W44 | 2021-W46 | 3 | 2 |
| 2021-W47 | 2021-W47 | 1 | -1 |
| 2021-W48 | 2021-W51 | 4 | 2 |
| 2021-W52 | 2021-W52 | 1 | 0 |
| 2022-W01 | 2022-W12 | 12 | 2 |
| 2022-W13 | 2022-W43 | 31 | 3 |
| 2022-W44 | 2022-W46 | 3 | 2 |
| 2022-W47 | 2022-W47 | 1 | -1 |
| 2022-W48 | 2023-W13 | 18 | 2 |
| 2023-W14 | 2023-W43 | 30 | 3 |
| 2023-W44 | 2023-W46 | 3 | 2 |
| 2023-W47 | 2023-W47 | 1 | -1 |
| 2023-W48 | 2024-W12 | 17 | 2 |
| 2024-W14 | 2024-W15 | 2 | 3 |
| 2024-W16 | 2024-W16 | 1 | 2 |
| 2024-W17 | 2024-W43 | 27 | 3 |
| 2024-W44 | 2024-W47 | 4 | 2 |
| 2024-W48 | 2024-W48 | 1 | 0 |
| 2024-W49 | 2025-W13 | 17 | 2 |
| 2025-W14 | 2025-W26 | 13 | 3 |
| 2025-W27 | 2025-W27 | 1 | -1 |
| 2025-W28 | 2025-W43 | 16 | 3 |
| 2025-W44 | 2025-W47 | 4 | 2 |
| 2025-W48 | 2025-W48 | 1 | 0 |
| 2025-W49 | 2026-W13 | 17 | 2 |
| 2026-W14 | 2026-W24 | 11 | 3 |
| 2026-W25 | 2026-W25 | 1 | -1 |
| 2026-W26 | 2026-W26 | 1 | 3 |
| 2026-W27 | 2026-W27 | 1 | -1 |
| 2026-W28 | 2026-W38 | 11 | 3 |

Weeks where both BTC-correlation and XAU-close estimates exist: 392; agreeing: 379. Disagreements concentrate in the US/EU DST gap weeks, where the gold close is not at 17:00 NY in server terms — i.e. the server clock follows EU DST, the gold session follows New York.


## Local timezone trap

The MetaTrader5 Python package converts **naive** `datetime` arguments using the PC's local timezone (here 'Middle East Standard Time', currently UTC+3 — coincidentally equal to the server offset). Rule: always pass integer epoch seconds on the server scale (or aware datetimes) and convert results with the model above; never pass naive datetimes.

