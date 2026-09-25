# MT5 — several processes on one terminal (P1.9)

_Generated 2026-09-25T08:38:03.417Z by `research/probes/probe_mt5_multiclient.py` from live/real data. Re-run to refresh._

Mode: automatic (worker 0 shuts down at 8 s).

| worker | successful reads | failed reads | re-inits | events |
|---|---|---|---|---|
| 0 | 159 | 0 | 0 | ('init', True, 20232911, 'WindsorBrokers1-Demo'); ('shutdown', 8.024889707565308); ('end', None) |
| 1 | 496 | 0 | 0 | ('init', True, 20232911, 'WindsorBrokers1-Demo'); ('end', 20232911) |
| 2 | 496 | 0 | 0 | ('init', True, 20232911, 'WindsorBrokers1-Demo'); ('end', 20232911) |

Distinct logins seen at attach: {20232911} — attaching without credentials must never switch the account.

