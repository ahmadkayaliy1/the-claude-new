# Agent guide

- **Read `PROJECT_STATUS.md` first.** It is the phase tracker (spec §10): overview, human actions pending,
  decision log, and every phase with its exact next step. Update it the moment a phase changes state;
  never delete decision-log entries (append a new one that supersedes).
- Spec: `AI_Trading_System_Spec_EN.md` (the master reference).
- Python: always the project venv — `.venv\Scripts\python.exe` (Python 3.12). The system `python` is 3.9.
- Run tests: `.venv\Scripts\python.exe -m pytest tests/unit -q` (integration tests need network/MT5: `-m integration`).
- CLI: `.venv\Scripts\python.exe -m tradingsystem --help`.
- Hard rules: no mock/simulated data on any path to a trading decision; every recommendation needs SL + risk;
  secrets only via `.env` (never logged); all timestamps int64 UTC ms, no naive datetimes (MT5 applies the local TZ to them).
- Never send MT5 orders outside the demo-test phases the user approved; default execution mode is `paper`.
