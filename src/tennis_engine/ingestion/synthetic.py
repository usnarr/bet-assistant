"""Strict synthetic sports fixture parser used only for SYS-03 demonstrations."""

from typing import Literal

from pydantic import Field

from tennis_engine.common.contracts import Contract, Identifier, Timestamp

from .contracts import ParsedItem


class SyntheticEvent(Contract):
    source_event_id: Identifier
    scheduled_start: Timestamp
    player_names: tuple[str, str]


class SyntheticSportsPayload(Contract):
    schema_version: Literal["1.0"]
    fixture_id: Identifier
    captured_at: Timestamp
    events: tuple[SyntheticEvent, ...] = Field(min_length=1)


class SyntheticSportsParser:
    """This parser cannot be registered as a real provider adapter."""

    version = "synthetic-sports-v1"

    def parse(self, body: bytes) -> tuple[ParsedItem, ...]:
        payload = SyntheticSportsPayload.model_validate_json(body)
        return tuple(
            ParsedItem(
                record_type="synthetic-source-event",
                natural_key=event.source_event_id,
                payload={
                    "schema_version": payload.schema_version,
                    "fixture_id": payload.fixture_id,
                    "captured_at": payload.captured_at.isoformat(),
                    "source_event_id": event.source_event_id,
                    "scheduled_start": event.scheduled_start.isoformat(),
                    "player_names": list(event.player_names),
                },
            )
            for event in payload.events
        )
