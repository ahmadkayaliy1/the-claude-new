"""Trading-session calendars (P1.10 measurements → P4.5).

Calendars are defined in the timezone that actually governs them (gold follows New York, so its UTC hours
move when US DST changes — independent of the broker's EU-DST server clock). Holidays are not modelled:
holiday closures surface as gaps that the source confirms as "no data" (known gaps, P2.8).
"""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

UTC = dt.timezone.utc
NY = ZoneInfo("America/New_York")


class SessionCalendar:
    name = "base"

    def is_open(self, utc_ms: int) -> bool:  # pragma: no cover - interface
        raise NotImplementedError


class AlwaysOpen(SessionCalendar):
    """Binance spot/futures: 24/7."""
    name = "always_open"

    def is_open(self, utc_ms: int) -> bool:
        return True


class NYMetalsFX(SessionCalendar):
    """Spot gold / FX CFDs: Sun 18:00 → Fri 17:00 New York, daily break 17:00–18:00 NY (Mon–Thu)."""
    name = "ny_metals_fx"

    def is_open(self, utc_ms: int) -> bool:
        t = dt.datetime.fromtimestamp(utc_ms / 1000, tz=UTC).astimezone(NY)
        wd, hm = t.weekday(), (t.hour, t.minute)
        if wd == 5:                                    # Saturday
            return False
        if wd == 6:                                    # Sunday: opens 18:00
            return hm >= (18, 0)
        if wd == 4:                                    # Friday: closes 17:00
            return hm < (17, 0)
        return not ((17, 0) <= hm < (18, 0))           # Mon–Thu daily break


class WindsorCryptoCFD(SessionCalendar):
    """Windsor crypto CFDs: 24/7 except weekly maintenance Saturday 05:00–08:00 UTC (measured, P1.10)."""
    name = "windsor_crypto_cfd"

    def is_open(self, utc_ms: int) -> bool:
        t = dt.datetime.fromtimestamp(utc_ms / 1000, tz=UTC)
        return not (t.weekday() == 5 and 5 <= t.hour < 8)


CALENDARS: dict[str, SessionCalendar] = {c.name: c for c in (AlwaysOpen(), NYMetalsFX(), WindsorCryptoCFD())}


def calendar_for(venue: str, symbol: str, asset_class: str) -> SessionCalendar:
    """Default calendar per instrument (overridable later via config if a new venue needs it)."""
    if venue.startswith("binance"):
        return CALENDARS["always_open"]
    if asset_class == "crypto":
        return CALENDARS["windsor_crypto_cfd"]
    return CALENDARS["ny_metals_fx"]
