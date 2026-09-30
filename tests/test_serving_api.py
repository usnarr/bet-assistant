"""SYS-12 F14 API contracts: decisions, filters, pagination, expiry, hard stops, permissions."""

import base64
import re
from datetime import timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from serving_support import (
    BOOK_SOURCE,
    READ_AT,
    FakeChecks,
    bet,
    build,
    copy,
    headers,
    no_bet,
    no_quote,
    stored,
    watch,
)
from settlement_support import MATCH_ID
from test_foundation_api import Probe
from test_pricing_decision import AT, fresh
from test_pricing_publication import observation

from tennis_engine.contracts.domain import RecommendationStatus
from tennis_engine.governance.contracts import Decision, Role
from tennis_engine.governance.service import PolicyLookup
from tennis_engine.infrastructure.settings import Settings
from tennis_engine.ingestion.bookmakers.quotes import ActionabilityReason
from tennis_engine.pricing.decision import prepare_publication
from tennis_engine.serving.api import create_app
from tennis_engine.serving.contracts import StoredDecision
from tennis_engine.serving.store import DecisionConflict, InMemoryDecisionStore

LIST = "/v1/tennis/recommendations"


def items(response):
    assert response.status_code == 200, response.text
    return response.json()["recommendations"]


def by_key(rows):
    return {row["decision_key"]: row for row in rows}


def error(response, status, code):
    assert response.status_code == status, response.text
    assert response.json()["error"]["code"] == code


# Authentication and permissions ---------------------------------------------------------


def test_every_route_requires_a_valid_token():
    client, *_ = build([stored(bet())])
    paths = [
        LIST,
        f"/v1/tennis/matches/{MATCH_ID}/analysis",
        f"/v1/audit/recommendations/{bet().decision_id}",
        "/v1/tennis/source-health",
    ]
    for path in paths:
        response = client.get(path)
        error(response, 401, "AUTHENTICATION_REQUIRED")
        assert "Basic" in response.headers["WWW-Authenticate"]
        error(
            client.get(path, headers={"Authorization": "Bearer wrong"}),
            401,
            "AUTHENTICATION_REQUIRED",
        )
    # A token in the query string is never accepted.
    error(client.get(f"{LIST}?token=synthetic-operator-token"), 401, "AUTHENTICATION_REQUIRED")


def test_basic_authentication_uses_the_token_as_password():
    client, *_ = build([stored(bet())])
    raw = base64.b64encode(b"viewer:synthetic-dashboard-token").decode()
    assert client.get(LIST, headers={"Authorization": f"Basic {raw}"}).status_code == 200
    bad = base64.b64encode(b"viewer:").decode()
    error(
        client.get(LIST, headers={"Authorization": f"Basic {bad}"}), 401, "AUTHENTICATION_REQUIRED"
    )


@pytest.mark.parametrize("role", [Role.DASHBOARD, Role.AGENT])
def test_viewer_roles_cannot_read_audit(role):
    record = bet()
    client, *_ = build([stored(record)])
    path = f"/v1/audit/recommendations/{record.decision_id}"
    error(client.get(path, headers=headers(role)), 403, "PERMISSION_DENIED")
    assert client.get(LIST, headers=headers(role)).status_code == 200


@pytest.mark.parametrize("role", [Role.OPERATOR, Role.POLICY_REVIEWER])
def test_internal_roles_read_audit(role):
    record = bet()
    client, *_ = build([stored(record)])
    response = client.get(f"/v1/audit/recommendations/{record.decision_id}", headers=headers(role))
    assert response.status_code == 200


def test_the_api_has_no_write_route():
    client, *_ = build([stored(bet())])
    for route in client.app.routes:
        methods = getattr(route, "methods", None) or set()
        assert methods <= {"GET", "HEAD"}, route
    for method in ("post", "put", "patch", "delete"):
        response = getattr(client, method)(LIST, headers=headers())
        assert response.status_code == 405


def test_missing_service_fails_closed():
    client = TestClient(create_app(Settings(environment="test"), Probe(True)))
    error(client.get(LIST, headers=headers()), 503, "SERVICE_NOT_CONFIGURED")
    assert client.get("/health/live").status_code == 200


def test_responses_are_not_cacheable():
    client, *_ = build([stored(bet())])
    response = client.get(LIST, headers=headers())
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Content-Type-Options"] == "nosniff"


# Decisions and numeric fidelity ---------------------------------------------------------


def test_every_decision_is_listed_with_exact_decimal_strings():
    records = {"bet": bet(), "watch": watch(), "no_bet": no_bet()}
    assert [r.status for r in records.values()] == list(RecommendationStatus)
    client, *_ = build([stored(r) for r in records.values()])
    body = client.get(LIST, headers=headers()).json()
    assert body["mode"] == "SHADOW" and body["view"] == "current"
    assert "places no bets" in body["notice"]
    assert body["responsible_use"]["allowed"] is True
    rows = by_key(body["recommendations"])
    for record in records.values():
        row = rows[record.decision_key]
        assert row["recorded_decision"] == row["decision"] == record.status.value
        assert row["recorded_stake"] == {"amount": str(record.stake.amount), "currency": "PLN"}
        assert row["displayed_odds"] == str(record.decimal_odds)
        assert row["probability"] == str(record.central_probability)
        assert row["probability_low"] == str(record.conservative_probability)
        assert row["break_even_probability"] == str(record.value.break_even_probability)
        assert row["expected_value"] == str(record.value.expected_value)
        assert row["conservative_expected_value"] == str(record.value.conservative_expected_value)
        assert row["conservative_roi"] == str(record.value.conservative_roi)
        assert row["cash_return_if_win"]["amount"] == str(record.value.cash_return_if_win.amount)
        assert row["failed_gates"] == [gate.value for gate in record.failed_gates]
        assert [reason["gate"] for reason in row["reasons"]] == row["failed_gates"]
        assert row["policy_versions"] == list(record.policy_versions)
        assert row["expires_at"] and row["generated_at"] and row["quote_observed_at"]
        assert row["quote_age_seconds"] == 15
        assert row["format"] == {"tour": "ATP", "draw_type": "SINGLES", "best_of": 3}
        assert row["virtual"] is True and row["automated_placement"] is False
        assert row["manual_quote_confirmation_required"] is True
    assert rows["decision-1"]["actionable"] is True
    assert rows["decision-1"]["recommended_stake"]["amount"] == str(records["bet"].stake.amount)
    assert Decimal(rows["decision-1"]["recommended_stake"]["amount"]) > 0
    for key in ("decision-watch", "decision-no-bet"):
        assert rows[key]["actionable"] is False
        assert rows[key]["recommended_stake"]["amount"] == "0.00"
        assert rows[key]["reasons"], "WATCH and NO_BET must list their reasons"


def test_unavailable_metrics_are_null_not_zero():
    record = no_quote()
    assert record.value is None and record.central_probability is None
    client, *_ = build([stored(record)])
    row = items(client.get(LIST, headers=headers()))[0]
    for name in (
        "displayed_odds",
        "cash_return_if_win",
        "break_even_probability",
        "expected_value",
        "conservative_expected_value",
        "quote_observed_at",
        "quote_age_seconds",
        "bookmaker",
        "probability",
        "probability_low",
    ):
        assert row[name] is None, name
    assert row["decision"] == "NO_BET"
    assert "identity_resolved" in row["failed_gates"]


# Filters and pagination -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("query", "code"),
    [
        ("decision=MAYBE", "INVALID_FILTER"),
        ("limit=0", "INVALID_FILTER"),
        ("limit=101", "INVALID_FILTER"),
        ("limit=ten", "INVALID_FILTER"),
        ("starts_after=2026-09-20T10:00:00", "INVALID_FILTER"),
        ("starts_after=not-a-time", "INVALID_FILTER"),
        (
            "starts_after=2026-09-21T10:00:00%2B00:00&starts_before=2026-09-20T10:00:00%2B00:00",
            "INVALID_FILTER",
        ),
        ("market=total_games", "UNSUPPORTED_MARKET"),
        ("bookmaker=unknown-book", "UNSUPPORTED_BOOKMAKER"),
        ("view=live", "INVALID_FILTER"),
        ("cursor=not-base64!", "INVALID_CURSOR"),
    ],
)
def test_invalid_filters_have_explicit_errors(query, code):
    client, *_ = build([stored(bet())])
    error(client.get(f"{LIST}?{query}", headers=headers()), 422, code)


def test_filters_select_bookmaker_decision_market_and_start_time():
    base = bet()
    other = copy(base, "other-book-bet", bookmaker="other-book")
    later = copy(no_bet(), "later")
    later_match = stored(later).context.match.model_copy(
        update={"scheduled_start": AT + timedelta(days=2)}
    )
    client, *_ = build(
        [
            stored(base),
            stored(other),
            stored(watch()),
            StoredDecision(
                record=later,
                context=stored(later).context.model_copy(update={"match": later_match}),
            ),
        ]
    )

    def keys(query):
        rows = items(client.get(f"{LIST}?{query}", headers=headers()))
        return sorted(row["decision_key"] for row in rows)

    assert keys("bookmaker=other-book") == ["other-book-bet"]
    assert keys("decision=WATCH&decision=NO_BET") == ["decision-watch", "later"]
    assert keys("market=match_winner") == keys("market=TENNIS_MATCH_WINNER")
    assert len(keys("market=match_winner")) == 4
    assert keys("starts_after=2026-09-21T00:00:00%2B00:00") == ["later"]
    assert "later" not in keys("starts_before=2026-09-21T00:00:00%2B00:00")


def test_decision_filter_matches_the_served_decision_and_recorded_decision_is_separate():
    checks = FakeChecks()
    client, *_ = build([stored(bet()), stored(watch()), stored(no_bet())], checks=checks)

    def keys(query, view="current"):
        rows = items(client.get(f"{LIST}?view={view}&{query}", headers=headers()))
        return sorted(row["decision_key"] for row in rows)

    assert keys("decision=BET") == [bet().decision_key]
    checks.disabled[BOOK_SOURCE] = "SOURCE_DISABLED"
    # The blocked BET and WATCH are served as NO_BET, so decision=BET finds nothing.
    assert keys("decision=BET") == []
    assert keys("decision=WATCH") == []
    assert keys("decision=NO_BET") == sorted(
        [bet().decision_key, "decision-watch", "decision-no-bet"]
    )
    # The recorded filter still finds the stored BET; the row shows both values.
    rows = items(client.get(f"{LIST}?recorded_decision=BET", headers=headers()))
    assert [(r["recorded_decision"], r["decision"]) for r in rows] == [("BET", "NO_BET")]
    assert keys("recorded_decision=BET&decision=NO_BET") == [bet().decision_key]
    assert keys("recorded_decision=BET&decision=WATCH") == []
    # History keeps the recorded decision, so both filters agree there.
    assert keys("decision=BET", view="history") == [bet().decision_key]
    error(client.get(f"{LIST}?recorded_decision=MAYBE", headers=headers()), 422, "INVALID_FILTER")


def test_served_filter_pages_are_complete_under_the_scan_limit(monkeypatch):
    import tennis_engine.serving.service as service_module

    class BlockSome(FakeChecks):
        def check(self, item, now):
            blocked = item.record.decision_key.startswith("skip")
            return ("SYNTHETIC_BLOCK",) if blocked else super().check(item, now)

    monkeypatch.setattr(service_module, "MAX_SCAN", 2)
    # Recorded WATCH rows that read checks block: the store cannot filter them out.
    records = [copy(watch(), f"skip-{index}") for index in range(5)]
    wanted = copy(watch(), "zz-watch")
    client, *_ = build([stored(r) for r in records] + [stored(wanted)], checks=BlockSome())
    found, cursor, pages = [], None, 0
    while True:
        url = f"{LIST}?decision=WATCH&limit=1" + (f"&cursor={cursor}" if cursor else "")
        body = client.get(url, headers=headers()).json()
        found += [row["decision_key"] for row in body["recommendations"]]
        pages += 1
        cursor = body["next_cursor"]
        if cursor is None:
            break
        assert pages < 10
    assert found == ["zz-watch"]
    # Each short page stopped at the scan limit and gave a cursor to continue.
    assert pages >= 3


def test_pagination_is_deterministic_and_complete():
    base = no_bet()
    records = [copy(base, f"page-{index}") for index in range(7)]
    client, *_ = build([stored(r) for r in reversed(records)])
    seen, cursor, pages = [], None, 0
    while True:
        url = f"{LIST}?limit=3" + (f"&cursor={cursor}" if cursor else "")
        body = client.get(url, headers=headers()).json()
        seen += [row["recommendation_id"] for row in body["recommendations"]]
        pages += 1
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert pages == 3
    assert seen == sorted(str(r.decision_id) for r in records)
    first = client.get(f"{LIST}?limit=3", headers=headers()).json()["next_cursor"]
    # A cursor is bound to its filters.
    error(
        client.get(f"{LIST}?limit=3&decision=BET&cursor={first}", headers=headers()),
        422,
        "INVALID_CURSOR",
    )


# Expiry, supersession and read-time hard stops -----------------------------------------


def test_expired_decision_leaves_the_current_list_but_stays_in_history_and_audit():
    record = bet()
    client, _, _, clock = build([stored(record)])
    clock.instant = record.expires_at
    assert items(client.get(LIST, headers=headers())) == []
    history = items(client.get(f"{LIST}?view=history", headers=headers()))
    assert len(history) == 1
    row = history[0]
    assert row["recorded_decision"] == row["decision"] == "BET"
    assert row["expired"] is True and row["actionable"] is False
    assert row["recommended_stake"]["amount"] == "0.00"
    assert row["recorded_stake"]["amount"] == str(record.stake.amount)
    assert "DECISION_EXPIRED" in row["read_time_reasons"]
    audit = client.get(f"/v1/audit/recommendations/{record.decision_id}", headers=headers())
    assert audit.json()["record"] == record.model_dump(mode="json")
    assert audit.json()["raw_payloads_included"] is False


def test_history_view_is_never_actionable():
    client, *_ = build([stored(bet())])
    row = items(client.get(f"{LIST}?view=history", headers=headers()))[0]
    assert row["decision"] == "BET" and row["actionable"] is False
    assert row["recommended_stake"]["amount"] == "0.00"


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"disabled": {BOOK_SOURCE: "SOURCE_DISABLED"}}, "SOURCE:synthetic-book:SOURCE_DISABLED"),
        (
            {
                "actionability": fresh(
                    actionable=False, reasons=(ActionabilityReason.QUOTE_SUSPENDED,)
                )
            },
            "RECHECK:QUOTE_SUSPENDED",
        ),
        ({"actionability": None}, "RECHECK:UNAVAILABLE"),
        (
            {
                "actionability": fresh(
                    observation=observation(), expires_at=AT + timedelta(seconds=1)
                )
            },
            "RECHECK:QUOTE_EXPIRED",
        ),
        (
            {"responsible": PolicyLookup(Decision.deny("GLOBAL_DISABLE"), None)},
            "RECHECK:GLOBAL_DISABLE",
        ),
    ],
)
def test_read_time_hard_stop_makes_a_cached_bet_not_actionable(change, reason):
    checks = FakeChecks()
    record = bet()
    client, *_ = build([stored(record)], checks=checks)
    assert items(client.get(LIST, headers=headers()))[0]["actionable"] is True
    for name, value in change.items():
        setattr(checks, name, value)
    row = items(client.get(LIST, headers=headers()))[0]
    assert row["recorded_decision"] == "BET" and row["decision"] == "NO_BET"
    assert row["actionable"] is False and row["recommended_stake"]["amount"] == "0.00"
    assert reason in row["read_time_reasons"]
    # The stored record does not change.
    audit = client.get(f"/v1/audit/recommendations/{record.decision_id}", headers=headers())
    assert audit.json()["record"]["status"] == "BET"


def test_watch_under_a_hard_stop_is_shown_as_no_bet():
    checks = FakeChecks()
    client, *_ = build([stored(watch())], checks=checks)
    checks.disabled = {BOOK_SOURCE: "SOURCE_DISABLED"}
    row = items(client.get(LIST, headers=headers()))[0]
    assert row["recorded_decision"] == "WATCH" and row["decision"] == "NO_BET"


def test_failed_read_check_fails_closed():
    checks = FakeChecks()
    checks.fail = True
    client, *_ = build([stored(bet())], checks=checks)
    row = items(client.get(LIST, headers=headers()))[0]
    assert row["actionable"] is False
    assert row["read_time_reasons"] == ["READ_CHECK_UNAVAILABLE"]


def test_every_current_read_rechecks_and_nothing_is_cached():
    checks = FakeChecks()
    client, *_ = build([stored(bet())], checks=checks)
    for _ in range(3):
        client.get(LIST, headers=headers())
    assert checks.calls == 3


def test_superseded_version_is_hidden_from_current_but_kept_in_audit():
    original = bet()
    outcome = prepare_publication(
        original,
        now=READ_AT,
        publication=Decision.deny("SOURCE_DISABLED"),
        actionability=None,
        responsible_use=FakeChecks().responsible,
        policy=None,  # type: ignore[arg-type] - unused on the publication-denied path
        rules=None,  # type: ignore[arg-type]
        reserve=lambda stake, allowed: UUID(int=1),
    )
    successor = outcome.record
    assert successor.supersedes == original.decision_id
    client, *_ = build([stored(original), stored(successor)])
    current = items(client.get(LIST, headers=headers()))
    assert [row["recommendation_id"] for row in current] == [str(successor.decision_id)]
    assert current[0]["decision"] == "NO_BET" and current[0]["version"] == 2
    history = items(client.get(f"{LIST}?view=history", headers=headers()))
    assert {row["recommendation_id"] for row in history} == {
        str(original.decision_id),
        str(successor.decision_id),
    }
    flags = {row["recommendation_id"]: row["superseded"] for row in history}
    assert flags == {str(original.decision_id): True, str(successor.decision_id): False}
    audit = client.get(f"/v1/audit/recommendations/{successor.decision_id}", headers=headers())
    assert audit.json()["version_chain"] == [str(original.decision_id), str(successor.decision_id)]
    first = client.get(f"/v1/audit/recommendations/{original.decision_id}", headers=headers())
    assert first.json()["superseded_by"] == [str(successor.decision_id)]
    assert first.json()["record"] == original.model_dump(mode="json")


# Missing entities, analysis and redistribution ------------------------------------------


def test_missing_entities_have_explicit_errors():
    client, *_ = build([stored(bet())])
    error(
        client.get(f"/v1/tennis/matches/{UUID(int=5)}/analysis", headers=headers()),
        404,
        "MATCH_NOT_FOUND",
    )
    error(
        client.get(f"/v1/audit/recommendations/{UUID(int=5)}", headers=headers()),
        404,
        "RECOMMENDATION_NOT_FOUND",
    )
    error(
        client.get("/v1/audit/recommendations/not-a-uuid", headers=headers()), 422, "INVALID_FILTER"
    )


def test_analysis_compares_players_and_lists_rejected_reasons():
    records = [bet(), watch(), no_bet()]
    client, *_ = build([stored(r) for r in records])
    body = client.get(f"/v1/tennis/matches/{MATCH_ID}/analysis", headers=headers()).json()
    assert body["match"]["players"][0]["display_name"] == "Player Alpha"
    rows = {row["key"]: row for row in body["comparison"]}
    assert rows["surface_elo"]["first_value"] == "1850.5"
    assert rows["surface_elo"]["second_value"] == "1778.0"
    assert rows["surface_elo"]["difference"] == "72.5"
    assert rows["minutes_48h"]["kind"] == "MISSING"
    assert rows["minutes_48h"]["first_value"] is None
    assert body["components"][0]["role"] == "PRIMARY"
    assert len(body["recommendations"]) == 3
    assert len(body["decision_quotes"]) == 3
    assert set(body["explanations"]) == {str(r.decision_id) for r in records}
    statuses = {row["decision"]: row["reasons"] for row in body["recommendations"]}
    assert statuses["WATCH"] and statuses["NO_BET"] and not statuses["BET"]
    assert {row["source_id"] for row in body["source_health"]} == {
        "synthetic-book",
        "synthetic-sports",
    }


def test_viewer_does_not_see_values_from_sources_without_redistribution_rights():
    client, *_ = build([stored(bet())], redistribution=())
    row = items(client.get(LIST, headers=headers(Role.DASHBOARD)))[0]
    assert row["displayed_odds"] is None and row["odds_withheld"] is True
    # Engine numbers stay visible; source values do not.
    assert row["probability"] is not None
    analysis = client.get(
        f"/v1/tennis/matches/{MATCH_ID}/analysis", headers=headers(Role.DASHBOARD)
    ).json()
    assert {row["kind"] for row in analysis["comparison"]} == {"WITHHELD"}
    assert all(row["first_value"] is None for row in analysis["comparison"])
    assert all(point["decimal_odds"] is None for point in analysis["decision_quotes"])
    text = " ".join(s["text"] for s in next(iter(analysis["explanations"].values())))
    assert "1850.5" not in text and "2.30" not in text
    # Internal roles see the values; the audit lists each source's right.
    internal = items(client.get(LIST, headers=headers(Role.OPERATOR)))[0]
    assert internal["displayed_odds"] == "2.30"
    audit = client.get(
        f"/v1/audit/recommendations/{bet().decision_id}", headers=headers(Role.OPERATOR)
    ).json()
    assert {item["redistribution_allowed"] for item in audit["source_rights"]} == {False}


def test_source_health_shows_disabled_and_stale_sources():
    checks = FakeChecks()
    client, _, _, clock = build([stored(bet())], checks=checks)
    health = {
        r["source_id"]: r for r in client.get("/v1/tennis/source-health", headers=headers()).json()
    }
    assert health["synthetic-book"]["status"] == "OK"
    assert health["synthetic-sports"]["status"] == "UNKNOWN"
    checks.disabled = {"synthetic-sports": "SOURCE_DISABLED"}
    clock.instant = READ_AT + timedelta(seconds=600)
    health = {
        r["source_id"]: r for r in client.get("/v1/tennis/source-health", headers=headers()).json()
    }
    assert health["synthetic-book"]["status"] == "STALE"
    assert health["synthetic-sports"] == health["synthetic-sports"] | {
        "status": "DISABLED",
        "reason": "SOURCE_DISABLED",
    }


# Store ---------------------------------------------------------------------------------


def test_store_is_append_only_and_idempotent():
    store = InMemoryDecisionStore()
    item = stored(bet())
    store.add(item)
    store.add(item)
    changed = stored(bet(), account_scope="other")
    with pytest.raises(DecisionConflict):
        store.add(changed)


def test_context_must_match_its_record():
    with pytest.raises(ValueError, match="another decision"):
        StoredDecision(record=bet(), context=stored(watch()).context)
    with pytest.raises(ValueError, match="quote"):
        stored(no_quote(), quote_source_id=BOOK_SOURCE, quote_observed_at=AT)


def test_no_decimal_money_field_is_a_json_number():
    client, *_ = build([stored(bet()), stored(watch())])
    raw = client.get(LIST, headers=headers()).text
    assert not re.search(r'"(amount|displayed_odds|probability|expected_value)":\s*[0-9]', raw)
