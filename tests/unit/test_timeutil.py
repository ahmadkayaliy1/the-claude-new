import datetime as dt

import pytest

from tradingsystem.core.timeframes import Timeframe
from tradingsystem.core.timeutil import (
    MS_PER_DAY, NaiveDatetimeError, from_ms, iso, parse_date_spec, to_ms,
)

UTC = dt.timezone.utc


def test_naive_datetime_rejected():
    with pytest.raises(NaiveDatetimeError):
        to_ms(dt.datetime(2026, 1, 1))


def test_roundtrip_exact_ms():
    d = dt.datetime(2026, 9, 25, 12, 34, 56, 789000, tzinfo=UTC)
    assert from_ms(to_ms(d)) == d
    assert iso(to_ms(d)) == "2026-09-25T12:34:56.789Z"


def test_offset_aware_is_converted_to_utc():
    plus3 = dt.timezone(dt.timedelta(hours=3))
    assert to_ms(dt.datetime(2026, 1, 1, 3, tzinfo=plus3)) == to_ms(dt.datetime(2026, 1, 1, tzinfo=UTC))


def test_parse_date_spec_variants():
    assert parse_date_spec("2017-08-17") == to_ms(dt.datetime(2017, 8, 17, tzinfo=UTC))
    assert parse_date_spec("2025-01-01T00:00:00Z") == to_ms(dt.datetime(2025, 1, 1, tzinfo=UTC))
    assert parse_date_spec("-2d", now=10 * MS_PER_DAY) == 8 * MS_PER_DAY
    assert parse_date_spec("earliest") == 0


def test_timeframe_floor_minutes_and_hours():
    t = to_ms(dt.datetime(2026, 9, 25, 13, 47, 31, tzinfo=UTC))
    assert Timeframe.M15.floor(t) == to_ms(dt.datetime(2026, 9, 25, 13, 45, tzinfo=UTC))
    assert Timeframe.H4.floor(t) == to_ms(dt.datetime(2026, 9, 25, 12, tzinfo=UTC))
    assert Timeframe.D1.floor(t) == to_ms(dt.datetime(2026, 9, 25, tzinfo=UTC))


def test_weekly_floor_is_monday_utc():
    friday = to_ms(dt.datetime(2026, 9, 25, 10, tzinfo=UTC))  # 2026-09-25 is a Friday
    assert from_ms(Timeframe.W1.floor(friday)) == dt.datetime(2026, 9, 21, tzinfo=UTC)


def test_timeframe_parse():
    assert Timeframe.parse("1h") is Timeframe.H1
    with pytest.raises(ValueError):
        Timeframe.parse("2h")
