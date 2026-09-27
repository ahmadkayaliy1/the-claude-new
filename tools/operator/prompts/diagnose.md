# Diagnosis $review_id

Now: $now_utc. The monitor (tools/monitor.py, no AI) found warnings and started you. Its findings: $reason

Window of the pack below: the last $hours hours. Pairs: $pairs. You have at most $max_turns turns and should need far
fewer: this is a short, cheap check, not a review. The monitor's own state is in the pack ("monitor state") and in
$data_root/shared/monitor_state.json.

1. Find the cause of each finding from the pack; run `tools/health_report.py --hours 1` (or with `--instance PAIR`)
   once if you need the current state. Classify each finding: transient and already recovered / needs the owner /
   ongoing harm.
2. Ongoing harm to money only - an order burst, a position without stop-loss that the system does not fix, repeated
   failed or duplicated orders, losses running toward the daily limit: engage that pair's kill switch with
   `tools/kill_switch.py --pair PAIR --reason '...'` (it only stops NEW orders; protective actions on open trades
   continue; only the owner can switch it off). One pair per diagnosis: the global switch is the monitor's (a large
   equity drop), and the tool refuses it here, a second pair and the last pair still trading (exit 2 - put in the
   summary what else the owner should stop). The monitor already engages switches for bursts and the daily loss
   limit - do not repeat what the pack shows as on.
3. A pair whose AI path is broken (repeated errors or invalid answers) may be paused with tools/tune.py
   `set pair.ai_paused_until +6h` (the policy may refuse; report it). Do not tune anything else in a diagnosis.
4. Something the owner must do (restart a system, the MT5 terminal, the VPN, sign the CLI in again): say exactly what
   and where (the script name) in the summary. A code or config fix → tools/propose.py (always from `main`).
5. End with the `## SUMMARY` block (level line, then at most $summary_max_chars characters): finding → cause →
   what you did → what the owner should do. `critical` only when money is at risk right now.

Use `$review_id` as `--review-id` for tune.py and propose.py.
