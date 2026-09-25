"""MT5 server-time model — rules measured in docs/exploration/mt5_time.md (P1.7)."""
import datetime as dt

import pytest

from tradingsystem.core.timeutil import MS_PER_HOUR, to_ms
from tradingsystem.ingest.mt5.servertime import (
    AmbiguousServerTime, MonotonicServerClock, NonexistentServerTime, ServerTimeModel,
)

UTC = dt.timezone.utc
M = ServerTimeModel()


def u(*a):
    return to_ms(dt.datetime(*a, tzinfo=UTC))


@pytest.mark.parametrize("when,expected", [
    (u(2026, 7, 15, 12), 3), (u(2026, 1, 15, 12), 2),      # EET/EEST era
    (u(2024, 3, 20, 12), 2),                                 # US DST already on, EU not yet → +2
    (u(2019, 7, 15, 12), 1), (u(2019, 1, 15, 12), 0),       # historical US-DST base-0 era
])
def test_offsets(when, expected):
    assert M.offset_hours(when) == expected


def test_eu_switch_instants_2026():
    assert M.offset_hours(u(2026, 3, 29, 0, 59)) == 2
    assert M.offset_hours(u(2026, 3, 29, 1, 0)) == 3
    assert M.offset_hours(u(2026, 10, 25, 0, 59)) == 3
    assert M.offset_hours(u(2026, 10, 25, 1, 0)) == 2


def test_roundtrip_normal_time():
    t = u(2026, 9, 25, 8, 28, 31)
    assert M.server_to_utc(M.utc_to_server(t)) == t
    assert M.utc_to_server(t) - t == 3 * MS_PER_HOUR


def test_fall_back_hour_is_ambiguous():
    server = u(2026, 10, 25, 3, 30)  # server wall clock 03:30 happens twice
    with pytest.raises(AmbiguousServerTime):
        M.server_to_utc(server)
    assert M.server_to_utc(server, prefer="earlier") == u(2026, 10, 25, 0, 30)
    assert M.server_to_utc(server, prefer="later") == u(2026, 10, 25, 1, 30)


def test_spring_forward_gap_does_not_exist():
    with pytest.raises(NonexistentServerTime):
        M.server_to_utc(u(2026, 3, 29, 3, 30))


def test_monotonic_clock_resolves_fall_back_by_order():
    clock = MonotonicServerClock(M)
    seq = [u(2026, 10, 25, 3, 58), u(2026, 10, 25, 3, 59), u(2026, 10, 25, 3, 0), u(2026, 10, 25, 3, 1),
           u(2026, 10, 25, 4, 0)]
    out = [clock.to_utc(s) for s in seq]
    assert out == [u(2026, 10, 25, 0, 58), u(2026, 10, 25, 0, 59), u(2026, 10, 25, 1, 0), u(2026, 10, 25, 1, 1),
                   u(2026, 10, 25, 2, 0)]
    assert out == sorted(out)


def test_regime_boundary_flagged_uncertain():
    assert M.is_uncertain(u(2020, 2, 5))
    assert not M.is_uncertain(u(2020, 3, 5))
    assert not M.is_uncertain(u(2019, 6, 1))
