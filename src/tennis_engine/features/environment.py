"""F08.6 environment and travel-proxy features (P1, gated feature set ``core-env-v1``).

Weather comes only from forecast records of explicitly permitted sources, issued by the
cutoff and valid at the start time known at the cutoff. Realized weather is rejected.
The permitted-source set is empty by default, because no weather source is approved in
the F01 register; the features are then missing with explicit flags, never filled.

Travel is a venue-timezone proxy: the absolute UTC-offset change between the edition of
the player's previous completed match and this edition. Arrival times are unknown, so the
feature is always flagged as a proxy.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Protocol
from uuid import UUID
from zoneinfo import ZoneInfo

from tennis_engine.common.contracts import Contract, Identifier, Timestamp
from tennis_engine.contracts.domain import Availability, FeatureValue
from tennis_engine.normalization.contracts import CourtEnvironment

from .asof import latest_available
from .contracts import FeatureDefinition, InputRef
from .core import CORE_SET
from .snapshots import FeatureContext, FeatureGroup, FeatureSet, fixed


class Forecast(Contract):
    """One archived forecast for an edition venue. ``realized`` marks observed weather."""

    forecast_id: Identifier
    edition_id: UUID
    source_id: Identifier
    issued_at: Timestamp
    valid_from: Timestamp
    valid_to: Timestamp
    temperature_c: Decimal | None = None
    wind_kph: Decimal | None = None
    precipitation_mm: Decimal | None = None
    realized: bool = False
    availability: Availability


class ForecastStore(Protocol):
    def forecasts(self, edition_id: UUID) -> Sequence[Forecast]: ...


@dataclass
class MemoryForecastStore:
    rows: list[Forecast] = field(default_factory=list)

    def add(self, forecast: Forecast) -> None:
        self.rows.append(forecast)

    def forecasts(self, edition_id: UUID) -> Sequence[Forecast]:
        return tuple(
            sorted(
                (item for item in self.rows if item.edition_id == edition_id),
                key=lambda item: (item.issued_at, item.forecast_id),
            )
        )


class EnvironmentConfig(Contract):
    version: Identifier = "environment-v1-candidate"
    permitted_sources: frozenset[Identifier] = frozenset()


def _definition(
    name: str, unit: str, direction: str, description: str, missing: str
) -> FeatureDefinition:
    return FeatureDefinition.model_validate(
        {
            "name": name,
            "unit": unit,
            "direction": direction,
            "description": description,
            "missing_behavior": missing,
        }
    )


ENVIRONMENT_DEFINITIONS: tuple[FeatureDefinition, ...] = (
    _definition("env.indoor", "flag", "MATCH", "Edition is indoor", "None if UNKNOWN"),
    _definition(
        "env.weather_applicable",
        "flag",
        "MATCH",
        "Outdoor venue, so weather can matter",
        "False for indoor; None if UNKNOWN",
    ),
    _definition(
        "env.forecast_temperature_c",
        "degC",
        "MATCH",
        "Latest permitted forecast issued by the cutoff and valid at the known start",
        "None plus env.weather_missing",
    ),
    _definition("env.forecast_wind_kph", "km/h", "MATCH", "Same forecast", "None"),
    _definition("env.forecast_precipitation_mm", "mm", "MATCH", "Same forecast", "None"),
    _definition(
        "env.forecast_age_hours", "hours", "MATCH", "Cutoff minus forecast issue time", "None"
    ),
    _definition(
        "env.weather_missing",
        "flag",
        "MATCH",
        "True if weather applies but no usable permitted forecast exists",
        "never None",
    ),
    *(
        _definition(
            f"{side}.timezone_shift_hours",
            "hours",
            "PLAYER_ONE" if side == "p1" else "PLAYER_TWO",
            "Venue-timezone proxy for travel since the previous completed match",
            "None if a timezone or previous match is unknown",
        )
        for side in ("p1", "p2")
    ),
    _definition(
        "env.travel_is_proxy",
        "flag",
        "MATCH",
        "Travel uses venue timezones, not arrivals",
        "never None",
    ),
)


def _offset_hours(zone: str | None, at: datetime) -> Decimal | None:
    if zone is None:
        return None
    offset = at.astimezone(ZoneInfo(zone)).utcoffset()
    if offset is None:
        return None
    return Decimal(int(offset.total_seconds())) / Decimal(3600)


def environment_group(forecasts: ForecastStore, config: EnvironmentConfig) -> FeatureGroup:
    def group(context: FeatureContext) -> dict[str, FeatureValue]:
        edition = context.edition
        values: dict[str, FeatureValue] = {}
        indoor = {
            CourtEnvironment.INDOOR: True,
            CourtEnvironment.OUTDOOR: False,
        }.get(edition.environment)
        values["env.indoor"] = indoor
        values["env.weather_applicable"] = None if indoor is None else not indoor
        schedule = context.view.schedule(context.match.match_id)
        start = schedule[0].scheduled_start if schedule else None
        forecast = None
        if indoor is False and start is not None:
            usable = [
                item
                for item in forecasts.forecasts(edition.edition_id)
                if item.source_id in config.permitted_sources
                and not item.realized
                and item.issued_at <= context.as_of
                and item.valid_from <= start < item.valid_to
            ]
            found = latest_available(usable, context.as_of, context.view.mode)
            if found is not None:
                forecast, proven = found
                context.use(
                    InputRef(
                        kind="forecast",
                        key=forecast.forecast_id,
                        version=1,
                        availability=proven,
                        observed_at=forecast.availability.observed_at,
                        source_id=forecast.source_id,
                    )
                )
        values["env.forecast_temperature_c"] = forecast.temperature_c if forecast else None
        values["env.forecast_wind_kph"] = forecast.wind_kph if forecast else None
        values["env.forecast_precipitation_mm"] = forecast.precipitation_mm if forecast else None
        values["env.forecast_age_hours"] = (
            fixed(Decimal(int((context.as_of - forecast.issued_at).total_seconds())) / 3600)
            if forecast
            else None
        )
        values["env.weather_missing"] = indoor is False and forecast is None
        for side, player in zip(("p1", "p2"), context.players, strict=True):
            history = context.view.completed_matches(
                player.player_id, exclude=context.match.match_id
            )
            shift = None
            if history and start is not None:
                previous = context.view.store.edition(history[-1].match.edition_id)
                before = _offset_hours(previous.timezone, history[-1].ended_at)
                now = _offset_hours(edition.timezone, start)
                if before is not None and now is not None:
                    shift = fixed(abs(now - before))
            values[f"{side}.timezone_shift_hours"] = shift
        values["env.travel_is_proxy"] = True
        return values

    return group


def environment_set(
    forecasts: ForecastStore, config: EnvironmentConfig | None = None
) -> FeatureSet:
    """``core-env-v1``: core-v1 plus environment. Enable only after F13 ablations (F08.8)."""
    config = config or EnvironmentConfig()
    return FeatureSet(
        version="core-env-v1",
        definitions=CORE_SET.definitions + ENVIRONMENT_DEFINITIONS,
        groups=(*CORE_SET.groups, ("environment", environment_group(forecasts, config))),
        config=CORE_SET.config | {"environment": config.model_dump_json()},
    )
