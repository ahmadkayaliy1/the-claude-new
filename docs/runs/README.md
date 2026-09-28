# docs/runs — generated evaluation reports

`demo.md` is written by `tools/demo_report.py` (Phase 5 A8): the demo window's evidence per pair and in total
(funnel, execution quality, realised PnL and costs, availability, incidents, AI cost per day, versions, tuning,
the sample-size statement). The implementing session generates and commits it at the end of a checkpoint from its
worktree, reading production read-only:

```
PYTHONPATH=src C:/the_claude_new/.venv/Scripts/python.exe tools/demo_report.py --root C:/the_claude_new
```

In the production checkout (`C:\the_claude_new`) do not write here: a changed file under `docs/` makes the checkout
dirty (the review packs flag it) and can block the next `git merge --ff-only`. `demo_report.py` refuses its default
output there (exit 3: this checkout's data root holds a system's `app.db` and no `--root` is given). Print it
instead, or write it under the data root (git-ignored):

```
.venv\Scripts\python.exe tools\demo_report.py --print
.venv\Scripts\python.exe tools\demo_report.py --out data\reviews\demo.md
.venv\Scripts\python.exe tools\go_live_inputs.py
```

The go-live table (`tools/go_live_inputs.py`) and its thresholds: docs/go_live_checklist.md.
