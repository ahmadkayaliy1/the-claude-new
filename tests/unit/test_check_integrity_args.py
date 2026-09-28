"""tools/check_integrity.py: the value of --since-hours is never taken as DATA_DIR, and a run that checked nothing
is never reported as OK (A9 of Phase 5 found the old parser printing OK for `--since-hours 48`)."""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def tool():
    spec = importlib.util.spec_from_file_location("ts_check_integrity", ROOT / "tools" / "check_integrity.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_since_hours_value_is_not_the_data_dir():
    t = tool()
    for argv in (["--since-hours", "48"], ["--since-hours", "48", "D:/x"], ["D:/x", "--since-hours", "48"]):
        a = t.parse_args(argv)
        assert a.since_hours == 48.0
        assert a.data_dir in (None, "D:/x")


def test_a_run_that_checks_nothing_is_not_ok(tmp_path, capsys):
    t = tool()
    assert t.main([str(tmp_path), "--since-hours", "1"]) == 2
    assert "nothing checked" in capsys.readouterr().out
    assert t.main([str(tmp_path / "missing")]) == 2
