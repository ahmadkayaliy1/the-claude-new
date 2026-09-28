"""`tradingsystem run --status` (what check_ops.bat prints for each pair): a venue collector that a pair has no
instrument on (XAUUSD on Binance spot) is shown as "n/a (no instrument)", not as a failure (Phase 5 A7)."""
from __future__ import annotations

from pathlib import Path

from tradingsystem.core.settings import PathsCfg, load_settings
from tradingsystem.ingest.common.appdb import AppDB
from tradingsystem.supervisor import control


def settings_for(tmp_path: Path, pair: str):
    s = load_settings(env_path=Path("nope.env"), extra_env={"TS_INSTANCE": pair})
    return s.model_copy(update={"paths": PathsCfg(data_dir=str(tmp_path / "data"), logs_dir=str(tmp_path / "logs"),
                                                  instance=pair)})


def test_a_venue_without_an_instrument_is_na_and_a_real_stop_is_shown(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(control.procs, "running_supervisors", lambda older_s=None: {})
    monkeypatch.setattr(control.procs, "find_terminals", lambda paths: {})
    s = settings_for(tmp_path, "XAUUSD")
    state = s.paths.state()
    state.mkdir(parents=True)
    db = AppDB(state / "app.db")
    db.set_status("binance_spot", "stopped")        # XAUUSD has no Binance spot instrument: stopped by design
    db.set_status("binance_usdm", "stopped")        # XAUUSDT perp proxy exists: a stop there is real
    db.set_status("mt5", "market_closed")
    db.close()
    control.status(state, s)
    out = capsys.readouterr().out
    assert "binance_spot         n/a (no instrument)" in out
    assert "binance_usdm         stopped" in out and "mt5                  market_closed" in out
