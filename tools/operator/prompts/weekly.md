# Weekly review $review_id

Now: $now_utc. Window: the last $hours hours (one week). Pairs: $pairs. You have at most $max_turns turns; the review
pack is below (also saved as $pack_md, structured data in $pack_json).

The daily reviews handle incidents; this review looks at the week as a whole. Checklist:

1. Health over the week: recurring problems (restarts, outages, stale data, log errors) and whether they are getting
   better or worse. One line per recurring problem, with counts.
2. Per pair: the funnel of the week and where it loses most; results (virtual and broker, TP1-first share, mean R,
   MFE/MAE, exits, `rejected_but_virtual_win`, costs: spread, adverse slippage (+ = against the trade), commission,
   swap — `-` means unknown, not zero) with sample sizes.
3. Attribution: the breakdowns by session, regime and setup kind. Which contexts win and which lose, with enough
   resolved outcomes to mean something (at least 10 per group; say "too few" otherwise).
4. The playbook of each pair: does it match what the week showed? The weekly review is the place to rewrite a
   playbook - short bullets on how to read this pair's market (sessions, structure, what failed and why), never about
   risk, sizing or stops. Only with at least 20 resolved outcomes of the pair in the window; otherwise leave it.
5. The tuning of the last week (tuning rows, changes, previous sessions): did each change do what its reason said?
   Revert (tools/tune.py revert) a change the numbers contradict; renew nothing without fresh evidence.
6. Versions: prompt / library / config / git hashes that changed during the week, and whether results moved with them.
7. AI usage of the week: tokens by role, cache-read share, the usage gauge; cost per placed trade if it can be computed.
8. Go-live evidence so far: the pack's "Go-live evidence so far" section is `tools/go_live_inputs.py` over the demo
   window (thresholds in docs/go_live_checklist.md). Read it as it is - do not re-judge an item or change a
   threshold: which items pass, which FAIL and why (the measured value with its n), which the owner still ticks. If
   the section says it is unavailable, say that instead of guessing.
9. Decide: at most one tune.py change per pair (a playbook rewrite counts as that pair's change), proposals for
   structural improvements with their numbers (at most two, none already open), notify only for urgent matters.
10. End with the `## SUMMARY` block (level line, then at most $summary_max_chars characters): the week in one line per
   pair, what you changed, the proposals, what the owner should decide - and, last, one paragraph that starts with
   "Go-live evidence so far:" (at most 400 characters, from step 8: the FAIL items with their numbers first, then how
   many pass, then what the owner still ticks; five demo days show no statistical edge, so never call a result one).

Use `$review_id` as `--review-id` for tune.py and propose.py.
