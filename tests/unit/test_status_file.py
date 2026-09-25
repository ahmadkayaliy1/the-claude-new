import runpy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_project_status_structure():
    mod = runpy.run_path(str(ROOT / "tools" / "check_status.py"))
    assert mod["check"](ROOT / "PROJECT_STATUS.md") == []
