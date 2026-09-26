"""Under the supervisor a service never launches a closed MT5 terminal itself (OPS-04 ingest-side guard): connect
refuses before ``initialize(path)`` is reached. No terminal is touched — the MetaTrader5 module is replaced."""
from types import SimpleNamespace

import pytest

from tradingsystem.core.settings import load_settings
from tradingsystem.ingest.mt5 import terminal as term_mod
from tradingsystem.ingest.mt5.terminal import MT5Terminal, MT5Unavailable


def make(tmp_path):
    s = load_settings(env_path=tmp_path / "nope.env")
    prof = s.mt5_data_profile().model_copy(update={"terminal_path": str(tmp_path / "terminal64.exe")})
    t = object.__new__(MT5Terminal)
    calls = []
    t.mt5 = SimpleNamespace(initialize=lambda **kw: calls.append(kw) or False, last_error=lambda: (-10005, "IPC"))
    t.profile, t.use_credentials, t.connected = prof, False, False
    return t, calls


def test_supervised_connect_never_launches_a_closed_terminal(tmp_path, monkeypatch):
    t, calls = make(tmp_path)
    monkeypatch.setenv(term_mod.NO_LAUNCH_ENV, "1")
    monkeypatch.setattr(term_mod, "terminal_running", lambda path, **kw: False)
    with pytest.raises(MT5Unavailable, match="not running"):
        t.connect()
    assert calls == []


def test_unsupervised_or_running_terminal_goes_ahead(tmp_path, monkeypatch):
    t, calls = make(tmp_path)
    monkeypatch.delenv(term_mod.NO_LAUNCH_ENV, raising=False)
    with pytest.raises(MT5Unavailable, match="initialize failed"):
        t.connect()
    monkeypatch.setenv(term_mod.NO_LAUNCH_ENV, "1")
    monkeypatch.setattr(term_mod, "terminal_running", lambda path, **kw: True)
    with pytest.raises(MT5Unavailable, match="initialize failed"):
        t.connect()
    assert len(calls) == 2


def test_a_terminal_that_just_started_is_not_attached_to(tmp_path, monkeypatch):
    """2026-09-26 16:57: attaching to a terminal still loading made initialize() launch a second copy as our child."""
    t, calls = make(tmp_path)
    monkeypatch.setenv(term_mod.NO_LAUNCH_ENV, "1")
    seen = []
    monkeypatch.setattr(term_mod, "terminal_running", lambda path, min_age_s=0.0: seen.append(min_age_s) or False)
    with pytest.raises(MT5Unavailable):
        t.connect()
    assert seen == [term_mod.NO_LAUNCH_MIN_AGE_S] and seen[0] >= 30 and calls == []


def test_terminal_age_is_checked(monkeypatch):
    import time as _time
    from types import SimpleNamespace as NS
    exe = "C:/Program Files/MetaTrader 5/terminal64.exe"
    procs = [NS(info={"name": "terminal64.exe", "create_time": _time.time() - 5}, exe=lambda: exe)]
    import psutil
    monkeypatch.setattr(psutil, "process_iter", lambda attrs=None: procs)
    assert term_mod.terminal_running(exe) is True
    assert term_mod.terminal_running(exe, min_age_s=30) is False
    procs[0].info["create_time"] = _time.time() - 60
    assert term_mod.terminal_running(exe, min_age_s=30) is True
