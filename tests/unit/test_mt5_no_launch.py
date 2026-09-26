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
    monkeypatch.setattr(term_mod, "terminal_running", lambda path: False)
    with pytest.raises(MT5Unavailable, match="not running"):
        t.connect()
    assert calls == []


def test_unsupervised_or_running_terminal_goes_ahead(tmp_path, monkeypatch):
    t, calls = make(tmp_path)
    monkeypatch.delenv(term_mod.NO_LAUNCH_ENV, raising=False)
    with pytest.raises(MT5Unavailable, match="initialize failed"):
        t.connect()
    monkeypatch.setenv(term_mod.NO_LAUNCH_ENV, "1")
    monkeypatch.setattr(term_mod, "terminal_running", lambda path: True)
    with pytest.raises(MT5Unavailable, match="initialize failed"):
        t.connect()
    assert len(calls) == 2
