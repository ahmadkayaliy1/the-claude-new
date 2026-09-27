# Daily review $review_id

Now: $now_utc. Window: the last $hours hours. Pairs: $pairs. You have at most $max_turns turns; the review pack is
below (also saved as $pack_md, structured data in $pack_json). Most days the pack alone is enough: aim for a few
turns, not all of them.

Work through this checklist, in this order:

1. Health. Every line starting with `!!`: what it is, since when, whether it still holds, what it costs (missed
   calls, stale data, a stopped system). A stopped or crashed system, a stale heartbeat, a position without stop-loss,
   a tripped drawdown stop or a kill switch that is on belongs at the top of the summary.
2. Funnel per pair: screen closes → triggers by strength → calls by role → stored answers (valid / invalid / error)
   → ideas → gate (which checks fail most) → placed → outcomes. Name the stage where most is lost and whether that is
   a healthy filter or a defect (errors, invalid answers, data warnings, `data_not_ready`, budget blocks).
3. Results: broker outcomes vs virtual outcomes, TP1-first share, mean R, MFE/MAE (stops too tight: MAE near -1 R
   while MFE is large; targets too far: TP1 rarely hit while MFE stays below 1 R), `rejected_but_virtual_win`, the
   NO_TRADE counterfactual. State the sample size with every number.
4. Position management: position actions and rule executions that were rejected, failed, deferred or dry-run.
5. Escalations: confirmed vs downgraded vs failed, and whether the downgrades were right (virtual outcome).
6. AI usage: calls and tokens by role, the cache-read share (below 0.5 means the prompt cache is not working), the
   usage gauge level.
7. Adaptive overlay: values in force, their expiry, the last changes and tuning rows. Did an earlier change help?
   (compare before/after only with enough samples). An entry that expires soon is renewed only with fresh evidence.
8. Decide:
   * at most ONE tune.py change per pair, only when step 3 shows a consistent effect with enough resolved outcomes
     (see the policy) - usually none; a refusal is fine, report it;
   * a proposal (tools/propose.py) for a defect or an improvement outside the overlay - only with numbers, and not
     when the pack lists the same proposal as open;
   * tools/notify.py only for something the owner must know before the summary arrives (rare).
9. End with the `## SUMMARY` block (level line, then at most $summary_max_chars characters).

Use `$review_id` as `--review-id` for tune.py and propose.py.
