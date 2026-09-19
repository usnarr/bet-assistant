import json
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from tennis_engine.common.clock import FrozenClock, SystemClock
from tennis_engine.common.contracts import Money
from tennis_engine.common.ids import new_id, stable_id
from tennis_engine.common.logging import REDACTED, JsonFormatter, sanitize


def test_clocks_are_utc_aware_and_frozen_clock_never_moves_backwards():
    assert SystemClock().now().tzinfo is UTC
    clock = FrozenClock(datetime(2026, 9, 19, 12, tzinfo=UTC))
    assert clock.advance(timedelta(seconds=1)) == datetime(2026, 9, 19, 12, 0, 1, tzinfo=UTC)
    with pytest.raises(ValueError, match="backwards"):
        clock.advance(timedelta(microseconds=-1))
    with pytest.raises(ValueError, match="Timezone-aware"):
        FrozenClock(datetime(2026, 9, 19, 12))


def test_ids_are_opaque_or_stable_as_requested():
    assert new_id() != new_id()
    assert stable_id("fixture", "one") == stable_id("fixture", "one")
    assert stable_id("fixture", "one") != stable_id("fixture", "two")
    with pytest.raises(ValueError):
        stable_id("", "one")


def test_money_uses_exact_decimals_and_json_strings():
    money = Money(amount="-1.20")
    assert money.amount == Decimal("-1.20")
    assert json.loads(money.model_dump_json()) == {"amount": "-1.20", "currency": "PLN"}
    for invalid in [1.2, "1.001", "NaN", True]:
        with pytest.raises(ValidationError):
            Money(amount=invalid)


def test_logging_redacts_nested_secrets_and_url_queries():
    value = sanitize(
        {
            "Authorization": "Bearer abc",
            "nested": {"api_key": "abc", "ok": "visible"},
            "url": "https://example.test/path?token=abc",
        }
    )
    assert value == {
        "Authorization": REDACTED,
        "nested": {"api_key": REDACTED, "ok": "visible"},
        "url": f"https://example.test/path?{REDACTED}",
    }
    record = logging.LogRecord("test", logging.INFO, "", 0, "safe", (), None)
    record.context = {"password": "do-not-log"}
    assert "do-not-log" not in JsonFormatter().format(record)
