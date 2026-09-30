"""F08.6 environment features: permitted sources only, cutoff rules and travel proxy."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pit_support import available, history

from tennis_engine.features.environment import (
    EnvironmentConfig,
    Forecast,
    MemoryForecastStore,
    environment_set,
)
from tennis_engine.features.snapshots import build_features
from tennis_engine.normalization.contracts import SourceTournamentRecord

START = datetime(2026, 9, 10, 18, tzinfo=UTC)
CUTOFF = START - timedelta(hours=2)
PERMITTED = EnvironmentConfig(permitted_sources=frozenset({"synthetic-weather"}))


@pytest.fixture
def state():
    h = history()
    h.warehouse.ingest_tournament(
        SourceTournamentRecord(
            source_id="synthetic-sports",
            source_tournament_id="ny",
            name="Fixture New York",
            season=2026,
            tour="ATP",
            surface="HARD",
            environment="OUTDOOR",
            timezone="America/New_York",
        )
    )
    h.warehouse.ingest_tournament(
        SourceTournamentRecord(
            source_id="synthetic-sports",
            source_tournament_id="hall",
            name="Fixture Hall",
            season=2026,
            tour="ATP",
            surface="HARD",
            environment="INDOOR",
            timezone="Europe/Warsaw",
        )
    )
    previous = START - timedelta(days=5)
    h.match(
        "warsaw",
        "alpha",
        "bravo",
        start=previous,
        observed=previous + timedelta(hours=3),
        winner="alpha",
    )
    h.target = h.match(
        "ny-1", "alpha", "bravo", start=START, observed=CUTOFF - timedelta(days=1), tournament="ny"
    )
    h.hall = h.match(
        "hall-1",
        "charlie",
        "delta",
        start=START,
        observed=CUTOFF - timedelta(days=1),
        tournament="hall",
    )
    h.edition = h.store.match(h.target).edition_id
    return h


def forecast(
    state,
    key,
    *,
    issued,
    observed=None,
    realized=False,
    source="synthetic-weather",
    temperature="21.5",
):
    return Forecast(
        forecast_id=key,
        edition_id=state.edition,
        source_id=source,
        issued_at=issued,
        valid_from=START - timedelta(hours=1),
        valid_to=START + timedelta(hours=1),
        temperature_c=Decimal(temperature),
        wind_kph=Decimal("12"),
        precipitation_mm=Decimal("0"),
        realized=realized,
        availability=available(observed or issued),
    )


def test_default_config_uses_no_weather_because_no_source_is_approved(state):
    store = MemoryForecastStore()
    store.add(forecast(state, "f-1", issued=CUTOFF - timedelta(hours=6)))
    row = build_features(state.store, state.target, CUTOFF, environment_set(store))
    assert row.values["env.forecast_temperature_c"] is None
    assert row.values["env.weather_missing"] is True
    assert row.values["env.travel_is_proxy"] is True


def test_permitted_forecast_before_cutoff_is_used_and_later_ones_are_ignored(state):
    store = MemoryForecastStore()
    store.add(forecast(state, "f-1", issued=CUTOFF - timedelta(hours=6)))
    fset = environment_set(store, PERMITTED)
    before = build_features(state.store, state.target, CUTOFF, fset)
    assert before.values["env.forecast_temperature_c"] == Decimal("21.5")
    assert before.values["env.forecast_age_hours"] == Decimal("6.000000")
    assert before.values["env.weather_missing"] is False
    assert any(item.kind == "forecast" for item in before.inputs)
    store.add(forecast(state, "f-late", issued=CUTOFF + timedelta(minutes=5), temperature="30"))
    store.add(
        forecast(
            state, "f-real", issued=CUTOFF - timedelta(hours=1), realized=True, temperature="35"
        )
    )
    store.add(
        forecast(
            state,
            "f-archived",
            issued=CUTOFF - timedelta(hours=1),
            observed=CUTOFF + timedelta(days=3),
            temperature="40",
        )
    )
    store.add(
        forecast(
            state,
            "f-other",
            issued=CUTOFF - timedelta(hours=1),
            source="unapproved-weather",
            temperature="10",
        )
    )
    assert build_features(state.store, state.target, CUTOFF, fset) == before


def test_indoor_venue_needs_no_weather(state):
    row = build_features(state.store, state.hall, CUTOFF, environment_set(MemoryForecastStore()))
    assert row.values["env.indoor"] is True
    assert row.values["env.weather_applicable"] is False
    assert row.values["env.weather_missing"] is False


def test_timezone_shift_is_a_flagged_venue_proxy(state):
    row = build_features(state.store, state.target, CUTOFF, environment_set(MemoryForecastStore()))
    match = state.store.match(state.target)
    for side in ("p1", "p2"):
        # Warsaw is UTC+2 and New York UTC-4 in September.
        assert row.values[f"{side}.timezone_shift_hours"] == Decimal("6.000000")
    assert match.player_ids
    indoor = build_features(state.store, state.hall, CUTOFF, environment_set(MemoryForecastStore()))
    assert indoor.values["p1.timezone_shift_hours"] is None
