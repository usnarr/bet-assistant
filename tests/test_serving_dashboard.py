"""F14.3/F14.4 dashboard: read-only HTML, exact numbers, visible NO_BET/WATCH, permissions."""

import base64
import re
from decimal import ROUND_FLOOR, Decimal

from serving_support import (
    BOOK_SOURCE,
    MATCH,
    FakeChecks,
    bet,
    build,
    headers,
    no_bet,
    no_quote,
    stored,
    watch,
)
from settlement_support import MATCH_ID

from tennis_engine.governance.contracts import Role
from tennis_engine.serving.contracts import PlayerRef

CENT = Decimal("0.01")


def html(client, path, role=Role.OPERATOR, status=200):
    response = client.get(path, headers=headers(role))
    assert response.status_code == status, response.text
    assert response.headers["content-type"].startswith("text/html")
    return response.text


def test_dashboard_requires_authentication_and_prompts_the_browser():
    client, *_ = build([stored(bet())])
    response = client.get("/dashboard")
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"].startswith("Basic")
    raw = base64.b64encode(b"viewer:synthetic-dashboard-token").decode()
    assert client.get("/dashboard", headers={"Authorization": f"Basic {raw}"}).status_code == 200


def test_list_shows_every_decision_with_reasons_and_exact_numbers():
    records = [bet(), watch(), no_bet()]
    client, *_ = build([stored(r) for r in records])
    text = html(client, "/dashboard")
    for record in records:
        assert f'<span class="decision {record.status.value}">{record.status.value}</span>' in text
        value = record.value
        # Exact values stay in the title; displayed values follow the documented rules.
        assert f'title="{value.break_even_probability}"' in text
        shown = (value.break_even_probability * 100).quantize(CENT)
        assert f">{shown}%<" in text
        floored = value.conservative_expected_value.quantize(CENT, rounding=ROUND_FLOOR)
        assert f">{floored} PLN<" in text
    stake = records[0].stake.amount
    assert f"{stake} PLN (virtual)" in text
    assert text.count("0.00 PLN <span") == 2  # WATCH and NO_BET show no stake
    assert "The conservative expected value is not positive." in text
    assert "The central expected value is not positive." in text
    assert "places no bets" in text and "Responsible-use limits" in text
    assert "Source health and data freshness" in text


def test_missing_values_are_not_shown_as_zero():
    client, *_ = build([stored(no_quote())])
    text = html(client, "/dashboard")
    assert "not available" in text
    assert "0.00%" not in text


def test_blocked_bet_is_labelled_as_blocked_at_read_time():
    checks = FakeChecks()
    checks.disabled = {BOOK_SOURCE: "SOURCE_DISABLED"}
    client, *_ = build([stored(bet())], checks=checks)
    text = html(client, "/dashboard")
    assert '<span class="decision NO_BET">NO_BET</span>' in text
    assert "recorded BET; blocked at read time" in text
    assert "SOURCE:synthetic-book:SOURCE_DISABLED" in text
    assert "(virtual)" not in text


def test_pages_are_read_only_accessible_and_script_free():
    client, *_ = build([stored(bet())])
    for path in (
        "/dashboard",
        f"/dashboard/matches/{MATCH_ID}",
        f"/dashboard/audit/{bet().decision_id}",
    ):
        response = client.get(path, headers=headers())
        text = response.text
        assert "<script" not in text.lower()
        assert re.findall(r"<form[^>]*>", text) in (
            [],
            ['<form class="filters" method="get" action="/dashboard" role="search">'],
        )
        assert '<html lang="en">' in text and 'href="#main"' in text
        assert "script-src" not in response.headers["Content-Security-Policy"]
        assert "default-src 'none'" in response.headers["Content-Security-Policy"]
        assert response.headers["Cache-Control"] == "no-store"
    list_page = html(client, "/dashboard")
    assert "<caption>" in list_page and 'scope="col"' in list_page
    assert client.post("/dashboard", headers=headers()).status_code == 405


def test_match_page_shows_comparison_explanation_and_role_aware_audit_link():
    records = [bet(), watch()]
    client, *_ = build([stored(r) for r in records])
    operator = html(client, f"/dashboard/matches/{MATCH_ID}")
    assert "Player Alpha vs Player Beta" in operator
    assert "+72.5 Elo points" in operator
    assert "Minutes played in the last 48 hours" in operator and "not available" in operator
    assert '<span class="kind">MISSING</span>' in operator
    assert '<span class="kind">INFERRED</span>' in operator
    assert f"/dashboard/audit/{records[0].decision_id}" in operator
    viewer = html(client, f"/dashboard/matches/{MATCH_ID}", Role.DASHBOARD)
    assert "/dashboard/audit/" not in viewer


def test_audit_page_is_internal_only():
    record = bet()
    client, *_ = build([stored(record)])
    denied = html(client, f"/dashboard/audit/{record.decision_id}", Role.DASHBOARD, 403)
    assert "PERMISSION_DENIED" in denied
    page = html(client, f"/dashboard/audit/{record.decision_id}")
    assert "Raw source payloads are not included" in page
    assert str(record.value.expected_value) in page
    html(client, f"/dashboard/matches/{record.decision_id}", status=404)


def test_viewer_sees_withheld_odds_without_rights():
    client, *_ = build([stored(bet())], redistribution=())
    text = html(client, "/dashboard", Role.DASHBOARD)
    assert "withheld (no redistribution right)" in text
    assert ">2.30<" not in text


def test_canonical_text_is_escaped():
    record = bet()
    hostile = MATCH.model_copy(
        update={
            "players": (
                PlayerRef(player_id=MATCH.players[0].player_id, display_name="<script>x</script>"),
                MATCH.players[1],
            )
        }
    )
    client, *_ = build([stored(record, match=hostile)])
    for path in ("/dashboard", f"/dashboard/matches/{MATCH_ID}"):
        text = html(client, path)
        assert "<script>x</script>" not in text and "&lt;script&gt;" in text


def test_invalid_dashboard_filters_render_an_error_page():
    client, *_ = build([stored(bet())])
    assert "INVALID_FILTER" in html(client, "/dashboard?decision=MAYBE", status=422)
    assert "INVALID_FILTER" in html(client, "/dashboard?view=live", status=422)
    assert "UNSUPPORTED_BOOKMAKER" in html(client, "/dashboard?bookmaker=nope", status=422)
