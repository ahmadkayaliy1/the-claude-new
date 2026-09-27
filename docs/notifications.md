# Notifications: log line, Windows toast, Telegram (Phase 4, §3.8)

The systems tell you what they do and what goes wrong without the Claude desktop app. Every notification is:

1. **always a log line** in the log of the process that sent it (logger `notify`);
2. a **Windows toast** on this PC (Action Center, shown as "Windows PowerShell");
3. a **Telegram message** to your phone, only once you set up a bot (H18, below; optional).

A notification never affects trading. If a toast or Telegram fails, the process logs one warning (at most one an hour
for each kind of failure) and carries on. The engine and the executor never wait for a notification: the toast and
the Telegram message are sent by a background thread of their own.

Code: `src/tradingsystem/core/notify.py` (`notify()`, `flush()`), `scripts/notify.ps1` (the toast),
`tools/notify.py` (the command line). Tests: `tests/unit/test_notify.py`.

---

## 1. H18: set up Telegram (optional, about 5 minutes)

Without Telegram you get the log line and the toast only. Everything else works the same.

1. **Create the bot.** In Telegram, open a chat with **@BotFather** (it has a blue verified tick) and send `/newbot`.
   Choose a display name (e.g. "My trading system") and a username that ends in `bot` (e.g. `ahmed_ts_alerts_bot`).
   BotFather replies with the bot's **token**, which looks like `1234567890:AAH...` (about 45 characters).
2. **Open your bot and start it.** Tap the `t.me/<username>` link BotFather gives you and press **Start** (or send any
   message). A bot can write only to a chat that has written to it first.
3. **Find your chat id.** In PowerShell (the token is typed, not saved in any history file):

   ```powershell
   $t = Read-Host "bot token"
   (Invoke-RestMethod "https://api.telegram.org/bot$t/getUpdates").result.message.chat | Select-Object id, username
   ```

   The `id` is your chat id (a number such as `123456789`). An empty result means the bot has no message yet: send it
   one and run the command again. For a group: add the bot to the group, write something in the group, and use the
   group's id (it is negative, e.g. `-1001234567890`).
4. **Add both to `.env`** in the checkout that runs (production: `C:\the_claude_new\.env`), one per line, with no
   quotes and no spaces:

   ```
   TELEGRAM_BOT_TOKEN=1234567890:AAH...
   TELEGRAM_CHAT_ID=123456789
   ```

   **No restart is needed:** every running system re-reads `.env` at most once a minute.
5. **Test it:**

   ```
   .venv\Scripts\python.exe tools\notify.py --level info --title "Test" --text "Hello from the trading system"
   ```

   It prints what happened to each channel, for example
   `notification info: "Test" - log: written; toast: shown; telegram: sent`. §6 lists the errors you may see.

**Keep the token secret.** Anyone who has it can send messages as your bot and read what is sent to the bot. It
belongs in `.env` only (`.env` is not in git). Never paste it into a chat, including one with Claude. If it leaks,
send `/revoke` to @BotFather and put the new token in `.env`. The system never writes the token or the chat id to a
log, an error message or its console output: both are masked (`****`).

**What leaves the PC.** Telegram messages pass through Telegram's servers. They contain what the notification says:
pair, direction, prices, profit or loss, reasons, review summaries. They never contain keys, passwords or tokens: the
text goes through the same masking as the logs before it is sent.

## 2. What is sent

The level decides how loud a notification is: `info` (something happened), `warn` (look at it soon), `critical`
(act now). The list follows §3.8. Where a notification has a dedupe key, the same key is sent only once in 30 minutes
by all systems together (§3).

| Source | Notifications | Level |
|---|---|---|
| Executor | order placed or failed; fills and closes; outcome; position action applied or rejected; management rule applied; drawdown stop tripped; kill switch on or off | info, warn for failures and rejections, critical for the drawdown stop |
| Engine | usage gauge level changed (key `gauge:<level>`); adaptive overlay invalid (warn) or an entry expired (info) | info or warn |
| Escalation (second opinion) | confirmed, downgraded to NO_TRADE, withheld, failed | info or warn |
| Monitor (`tools\monitor.py`, every 15 min) | every finding: stale heartbeat, MT5 IPC hung, position without SL, order burst, daily loss, equity drop, low RAM or disk, restart loop, outage, VPN change, overdue review (key `monitor:<finding>`) | as in docs/monitoring.md |
| Kill switch (`tools\kill_switch.py`, dashboard) | switch engaged, or could not be engaged | critical |
| Tuning (`tools\tune.py`) | a change applied or reverted | info |
| Proposals (`tools\propose.py`) | a proposal is waiting for you | info |
| Operator sessions | the daily, weekly or diagnosis summary; a failed session; `review_touched_checkout` | info or warn |
| Scripts and sessions | anything sent with `tools\notify.py` | as given |

The toast title starts with `Warning:` or `CRITICAL:` for those levels, and a critical toast stays on screen longer.
A Telegram message starts with `[INFO]`, `[WARN]` or `[CRITICAL]`. When the title does not already name the pair,
the pair (or the pair system that sent it) is added, because three systems share one chat.

## 3. Rules and limits

The rules are checked in this order:

| Rule | Default (`notify:` in `config/config.yaml`) | Effect |
|---|---|---|
| `TS_NOTIFY_DISABLE=1` in the environment | not set | log line only (the unit tests set it; so can you for one run) |
| `enabled` | `true` | `false`: log line only |
| `min_level` | `info` | a lower level: log line only |
| `toast` / `toast_min_level` | `true` / `info` | no toast, or toasts only from that level up |
| `telegram` | `true` | `false`: no Telegram even when the token is set |
| `rate_per_hour` | `20` per process | above it: log line only, and one warning an hour in that process's log |
| `dedupe_minutes` | `30` | the same key within this time, from any system: sent once (`0` = no dedupe) |
| `timeout_s` | `10` | the Telegram request; a toast gets at least 10 s |

* **Dedupe** works across processes through `data\shared\notify_state.json` (under the file lock
  `data\shared\locks\notify_state.lock`). Only notifications that carry a key are deduplicated. A key is recorded when
  the message is sent: if Telegram then fails, the other systems still stay quiet for that key until the time is up.
  Notifications without a key (a fill, an order) are never deduplicated.
* **Rate limit** counts what a process actually sent in the last hour. A deduplicated notification does not count.
* **No retries.** A message that fails is not sent again. The log line is always there.
* **Short-lived tools** (`tools\notify.py`, the monitor, tune, propose, the session runner) wait for the toast and
  Telegram before they exit, at most 15 s.

The `notify:` block (all keys; defaults shown):

```yaml
notify:
  enabled: true
  toast: true
  telegram: true            # used only when TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set
  min_level: info           # info | warn | critical
  toast_min_level: info
  rate_per_hour: 20         # per process
  dedupe_minutes: 30
  timeout_s: 10
```

## 4. How to silence it

| You want | Do this | Takes effect |
|---|---|---|
| no Telegram for a while | mute the bot's chat in Telegram | at once |
| no Telegram at all | remove the two `TELEGRAM_*` lines from `.env` | within a minute, no restart |
| no toasts | Windows Settings > System > Notifications > turn off "Windows PowerShell" (or Do not disturb) | at once |
| no toasts, config way | `notify: {toast: false}` in `config/config.local.yaml` | after `scripts\restart_all.bat` |
| only warnings and worse | `notify: {min_level: warn}` (or `toast_min_level: warn` for the toasts only) | after a restart |
| nothing but log lines | `notify: {enabled: false}` | after a restart |
| one silent run of a tool | `set TS_NOTIFY_DISABLE=1` in that console first | that console only |

`config/config.local.yaml` may hold only **one** `notify:` block; put every key you change inside it. Remove the block
before you go back to code older than Phase 4 (older code refuses unknown keys). Tools such as the monitor read the
config at every run, so for them a change takes effect at their next run.

## 5. Where to find past notifications

Each notification is a line in the log of the process that sent it: `logs\<PAIR>\engine.jsonl`,
`logs\<PAIR>\executor.jsonl`, `logs\monitor.jsonl`, `logs\notify.jsonl` (from `tools\notify.py`), and so on. The line
has `"logger": "notify"` and a `ctx` with `level`, `key` and `pair`. A critical notification is written at level
WARNING with the prefix `[CRITICAL]`, so the health report does not count it as an error. To list them:

```powershell
Select-String -Path logs\*.jsonl, logs\*\*.jsonl -Pattern '"logger": "notify"' | Select-Object -Last 20
```

Problems with a channel are also lines with `"logger": "notify"`: `Telegram sendMessage failed: ...`,
`toast failed: ...`, `notification rate limit ... reached`.

## 6. Troubleshooting

| `tools\notify.py` prints | Meaning | Fix |
|---|---|---|
| `telegram: skipped (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set)` | one of the two is missing | check the names and that `.env` is in the checkout that runs |
| `telegram: failed: HTTP 401 Unauthorized` | wrong or revoked token | copy the token again from @BotFather |
| `telegram: failed: HTTP 400 Bad Request: chat not found` | wrong chat id, or the bot was never started | §1 steps 2 and 3 |
| `telegram: failed: HTTP 403 Forbidden: bot was blocked by the user` | you blocked the bot | unblock it in Telegram |
| `telegram: failed: HTTP 429 ...` | Telegram's own flood limit | wait; lower `rate_per_hour` |
| `telegram: failed: ConnectError ...` or `ConnectTimeout ...` | no internet, VPN or firewall | the log line and the toast still work |
| `toast: shown` but nothing appears | Windows hides it: Do not disturb, or notifications off for Windows PowerShell | Windows Settings > System > Notifications |
| `toast: failed: ...` | PowerShell or WinRT failed (e.g. no desktop session) | run `tools\notify.py` from your own logged-on desktop |
| `log only (TS_NOTIFY_DISABLE)` | the variable is set in this environment | `set TS_NOTIFY_DISABLE=` |
| `... rate limit 20/hour: log only` | this process sent 20 in the last hour | wait, or raise `rate_per_hour` |
| `... deduped (key sent within 30 min)` | the same key was sent recently by some system | expected |

`tools\notify.py` exits with 0 whenever the log line was written, even if a channel failed; 1 when the config cannot be
loaded; 3 for an invalid request (e.g. an unknown `--level`). `scripts\notify.ps1 -TitleB64 <b64> -TextB64 <b64>
-Check` builds a toast without showing it (a self-test).
