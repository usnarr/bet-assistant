"""F14.3/F14.4 read-only dashboard. Server-rendered HTML, no JavaScript, no forms that write.

The pages render the same views as the API. Display formatting is fixed and documented:

- Money and odds: the exact stored decimal string.
- Probabilities: a percentage with two decimals, rounded half-even.
- Expected value: floored to whole grosze, so the page never overstates value.
- Return (ROI): a percentage with two decimals, floored.

Each formatted number carries the exact stored value in its `title` attribute. A missing
value is shown as "not available", never as zero.
"""

import json
from collections.abc import Callable, Iterable
from decimal import ROUND_FLOOR, ROUND_HALF_EVEN, Decimal
from html import escape
from typing import Annotated, Any
from urllib.parse import urlencode
from uuid import UUID

from fastapi import Depends, FastAPI, Query, Response
from fastapi.responses import HTMLResponse

from tennis_engine.common.contracts import Money
from tennis_engine.contracts.domain import RecommendationStatus
from tennis_engine.governance.contracts import Principal

from .auth import Permission, allowed
from .contracts import (
    MatchAnalysis,
    RecommendationPage,
    RecommendationView,
    SourceHealth,
    Statement,
)
from .service import ApiError, RecommendationFilter, RecommendationService

CSP = (
    "default-src 'none'; style-src 'self'; img-src 'none'; form-action 'self'; "
    "base-uri 'none'; frame-ancestors 'none'"
)
NOT_AVAILABLE = '<span class="na">not available</span>'
CENT = Decimal("0.01")

STYLE = """
:root { --fg: #1b1b1b; --bg: #ffffff; --muted: #595959; --line: #c8c8c8;
  --bet: #0b5d1e; --watch: #7a4b00; --nobet: #8a1c1c; --band: #f3f3f3; }
@media (prefers-color-scheme: dark) {
  :root { --fg: #ececec; --bg: #161616; --muted: #b0b0b0; --line: #4a4a4a;
    --bet: #7fd08f; --watch: #f0c060; --nobet: #f19a9a; --band: #222222; } }
body { font: 15px/1.45 system-ui, sans-serif; color: var(--fg); background: var(--bg);
  margin: 0 auto; padding: 0 16px 32px; max-width: 1200px; }
a { color: inherit; }
.skip { position: absolute; left: -9999px; } .skip:focus { left: 16px; top: 8px; }
.banner { border: 2px solid var(--fg); padding: 8px 12px; margin: 12px 0; }
.status { color: var(--muted); }
.table-wrap { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; margin: 8px 0 20px; }
caption { text-align: left; font-weight: 600; padding: 4px 0; }
th, td { border-bottom: 1px solid var(--line); padding: 6px 8px; text-align: left;
  vertical-align: top; }
thead th { background: var(--band); }
td.num { font-variant-numeric: tabular-nums; white-space: nowrap; }
.decision { font-weight: 700; border: 2px solid; padding: 1px 6px; white-space: nowrap; }
.BET { color: var(--bet); } .WATCH { color: var(--watch); } .NO_BET { color: var(--nobet); }
.na { color: var(--muted); font-style: italic; }
ul.reasons { margin: 0; padding-left: 18px; }
.kind { font-weight: 600; font-size: 12px; letter-spacing: .03em; }
pre { background: var(--band); padding: 8px; overflow-x: auto; }
form.filters { display: flex; flex-wrap: wrap; gap: 12px; align-items: end; }
"""


# Formatting ------------------------------------------------------------------------------


def _titled(text: str, exact: object) -> str:
    return f'<span title="{escape(str(exact))}">{escape(text)}</span>'


def percent(value: Decimal | None, rounding: str = ROUND_HALF_EVEN) -> str:
    if value is None:
        return NOT_AVAILABLE
    shown = (value * 100).quantize(CENT, rounding=rounding)
    return _titled(f"{shown}%", value)


def money(value: Money | None, suffix: str = "") -> str:
    if value is None:
        return NOT_AVAILABLE
    return escape(f"{value.amount} {value.currency.value}{suffix}")


def floored_money(value: Decimal | None) -> str:
    if value is None:
        return NOT_AVAILABLE
    return _titled(f"{value.quantize(CENT, rounding=ROUND_FLOOR)} PLN", value)


def exact(value: object | None) -> str:
    return NOT_AVAILABLE if value is None else escape(str(value))


def timestamp(value: Any) -> str:
    if value is None:
        return NOT_AVAILABLE
    return f'<time datetime="{escape(value.isoformat())}">{escape(value.isoformat())}</time>'


def decision_badge(view: RecommendationView) -> str:
    badge = f'<span class="decision {view.decision.value}">{view.decision.value}</span>'
    if view.recorded_decision != view.decision:
        badge += (
            f' <span class="status">(recorded {view.recorded_decision.value}; '
            "blocked at read time)</span>"
        )
    return badge


def reason_list(view: RecommendationView) -> str:
    lines = [escape(reason.text) for reason in view.reasons]
    lines += [f"Read-time check: {escape(code)}" for code in view.read_time_reasons]
    if not lines:
        return '<span class="status">Every gate passed.</span>'
    return '<ul class="reasons">' + "".join(f"<li>{line}</li>" for line in lines) + "</ul>"


def stake_cell(view: RecommendationView) -> str:
    if view.actionable:
        return money(view.recommended_stake, " (virtual)")
    return '0.00 PLN <span class="status">(no stake)</span>'


# Page frame ----------------------------------------------------------------------------


def page(title: str, body: str, status_code: int = 200) -> HTMLResponse:
    html = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{escape(title)}</title>"
        '<link rel="stylesheet" href="/dashboard/static/style.css"></head><body>'
        '<a class="skip" href="#main">Skip to content</a>'
        '<header><p><a href="/dashboard">Tennis Engine dashboard</a> '
        '<span class="status">read-only, shadow mode</span></p></header>'
        f'<main id="main"><h1>{escape(title)}</h1>{body}</main></body></html>'
    )
    return HTMLResponse(html, status_code=status_code, headers={"Content-Security-Policy": CSP})


def error_page(error: ApiError) -> HTMLResponse:
    body = f"<p>{escape(error.code)}: {escape(error.detail)}</p>"
    return page("Request not completed", body, error.status)


def table(caption: str, headers: Iterable[str], rows: Iterable[Iterable[str]]) -> str:
    head = "".join(f'<th scope="col">{escape(item)}</th>' for item in headers)
    body = "".join("<tr>" + "".join(rows_cell for rows_cell in row) + "</tr>" for row in rows)
    return (
        f'<div class="table-wrap"><table><caption>{escape(caption)}</caption>'
        f"<thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>"
    )


def cell(content: str, numeric: bool = False) -> str:
    return f'<td class="num">{content}</td>' if numeric else f"<td>{content}</td>"


def banner(notice: str) -> str:
    return f'<p class="banner" role="note">{escape(notice)}</p>'


def health_table(rows: Iterable[SourceHealth]) -> str:
    return table(
        "Source health and data freshness",
        ["Source", "Status", "Reason", "Latest quote observation", "Age (seconds)"],
        (
            [
                cell(escape(row.source_id)),
                cell(f"<strong>{escape(row.status)}</strong>"),
                cell(escape(row.reason)),
                cell(timestamp(row.latest_observation_at)),
                cell(exact(row.age_seconds), True),
            ]
            for row in rows
        ),
    )


# Pages ---------------------------------------------------------------------------------


def odds_cell(view: RecommendationView) -> str:
    if view.odds_withheld:
        return '<span class="na">withheld (no redistribution right)</span>'
    return exact(view.displayed_odds)


def list_body(
    result: RecommendationPage, request: RecommendationFilter, health: Iterable[SourceHealth]
) -> str:
    responsible = result.responsible_use
    state = "available" if responsible.allowed else "NOT available"
    parts = [
        banner(result.notice),
        f'<p class="status">Responsible-use limits for account {escape(responsible.account_scope)}:'
        f" <strong>{state}</strong> ({escape(responsible.reason)}; policy "
        f"{escape(responsible.policy_version or 'none')}). Generated "
        f"{timestamp(result.generated_at)}.</p>",
        _filters(request),
        health_table(health),
    ]
    rows = []
    for view in result.recommendations:
        rows.append(
            [
                cell(
                    f'<a href="/dashboard/matches/{view.match_id}">{escape(view.event)}</a><br>'
                    f'<span class="status">{escape(view.tournament)}, {view.format.tour.value} '
                    f"singles, best of {view.format.best_of}</span>"
                ),
                cell(timestamp(view.scheduled_start)),
                cell(exact(view.bookmaker)),
                cell(exact(view.selection)),
                cell(odds_cell(view), True),
                cell(decision_badge(view)),
                cell(percent(view.probability), True),
                cell(percent(view.probability_low), True),
                cell(percent(view.break_even_probability), True),
                cell(floored_money(view.conservative_expected_value), True),
                cell(percent(view.conservative_roi, ROUND_FLOOR), True),
                cell(stake_cell(view), True),
                cell(money(view.maximum_stake), True),
                cell(exact(view.quote_age_seconds), True),
                cell(timestamp(view.expires_at)),
                cell(reason_list(view)),
            ]
        )
    caption = f"{len(rows)} {'current' if result.view == 'current' else 'historical'} records"
    parts.append(
        table(
            caption,
            [
                "Match",
                "Start (UTC)",
                "Bookmaker",
                "Selection",
                "Odds",
                "Decision",
                "Probability",
                "Conservative probability",
                "Net break-even",
                "Conservative EV",
                "Conservative return",
                "Recommended stake",
                "Maximum stake",
                "Quote age (s)",
                "Expires (UTC)",
                "Reasons",
            ],
            rows,
        )
        if rows
        else "<p>No records match these filters.</p>"
    )
    if result.next_cursor:
        query = _query(request) | {"cursor": result.next_cursor}
        parts.append(
            f'<p><a href="/dashboard?{escape(urlencode(query, doseq=True))}">Next page</a></p>'
        )
    return "".join(parts)


def _query(request: RecommendationFilter) -> dict[str, Any]:
    query: dict[str, Any] = {"view": request.view, "limit": request.limit}
    if request.bookmaker:
        query["bookmaker"] = request.bookmaker
    if request.statuses:
        query["decision"] = sorted(request.statuses)
    return query


def _filters(request: RecommendationFilter) -> str:
    options = "".join(
        f'<option value="{name}"{" selected" if request.view == name else ""}>{name}</option>'
        for name in ("current", "history")
    )
    boxes = "".join(
        f'<label><input type="checkbox" name="decision" value="{status.value}"'
        f"{' checked' if status in request.statuses else ''}> {status.value}</label> "
        for status in RecommendationStatus
    )
    return (
        '<form class="filters" method="get" action="/dashboard" role="search">'
        f'<label>View <select name="view">{options}</select></label>'
        f'<label>Bookmaker <input name="bookmaker" value="{escape(request.bookmaker or "")}">'
        f"</label><fieldset><legend>Decision shown after read checks</legend>{boxes}</fieldset>"
        '<button type="submit">Filter</button></form>'
    )


def statement_list(statements: Iterable[Statement]) -> str:
    items = "".join(
        f'<li><span class="kind">{escape(item.kind)}</span> {escape(item.text)}</li>'
        for item in statements
    )
    return f'<ul class="reasons">{items}</ul>'


def analysis_body(result: MatchAnalysis, can_audit: bool) -> str:
    match = result.match
    first, second = match.players
    parts = [
        banner(
            "Shadow mode. All records are virtual. Confirm the quote manually before any "
            "action. This system places no bets."
        ),
        f"<p>{escape(match.tournament_name)}, {match.tour.value} singles, best of "
        f"{match.best_of}, surface {escape(match.surface or 'unknown')}. Scheduled start "
        f"{timestamp(match.scheduled_start)}.</p>",
    ]
    parts.append(
        table(
            "Player comparison (canonical facts)",
            ["Fact", "Kind", first.display_name, second.display_name, "Difference", "As of"],
            (
                [
                    cell(escape(row.label)),
                    cell(escape(str(row.kind))),
                    cell(_unit(row.first_value, row.unit), True),
                    cell(_unit(row.second_value, row.unit), True),
                    cell(_unit(row.difference, row.unit, signed=True), True),
                    cell(timestamp(row.as_of)),
                ]
                for row in result.comparison
            ),
        )
    )
    parts.append(
        table(
            "Model components",
            ["Role", "Component", "Version", "Probability"],
            (
                [
                    cell(escape(item.role)),
                    cell(escape(item.model.component)),
                    cell(escape(item.model.version)),
                    cell(percent(item.probability), True),
                ]
                for item in result.components
            ),
        )
    )
    for view in result.recommendations:
        audit = (
            f' <a href="/dashboard/audit/{view.recommendation_id}">Audit record</a>'
            if can_audit
            else ""
        )
        parts.append(
            f"<section><h2>{escape(view.bookmaker or 'No bookmaker')}: "
            f"{escape(view.selection or 'no selection')} {decision_badge(view)}</h2>"
            f"<p>Recommended stake: {stake_cell(view)}. Odds: {odds_cell(view)}. "
            f"Net break-even: {percent(view.break_even_probability)}. Conservative EV: "
            f"{floored_money(view.conservative_expected_value)}. Payout if win: "
            f"{money(view.cash_return_if_win)}. Quote age: {exact(view.quote_age_seconds)} s. "
            f"Expires {timestamp(view.expires_at)}.{audit}</p>"
            f"<h3>Reasons</h3>{reason_list(view)}"
            f"<h3>Explanation</h3>"
            f"{statement_list(result.explanations.get(str(view.recommendation_id), ()))}"
            "</section>"
        )
    parts.append(
        table(
            "Quotes used by decisions (market movement)",
            ["Observed (UTC)", "Bookmaker", "Selection", "Odds"],
            (
                [
                    cell(timestamp(point.observed_at)),
                    cell(escape(point.bookmaker)),
                    cell(escape(_name(result, point.selection_player_id))),
                    cell(
                        '<span class="na">withheld</span>'
                        if point.odds_withheld
                        else exact(point.decimal_odds),
                        True,
                    ),
                ]
                for point in result.decision_quotes
            ),
        )
    )
    parts.append(health_table(result.source_health))
    return "".join(parts)


def _name(result: MatchAnalysis, player_id: UUID) -> str:
    player = result.match.player(player_id)
    return player.display_name if player else str(player_id)


def _unit(value: Decimal | None, unit: str, signed: bool = False) -> str:
    if value is None:
        return NOT_AVAILABLE
    text = f"+{value}" if signed and value > 0 else str(value)
    return escape(f"{text} {unit}".strip())


# Routes --------------------------------------------------------------------------------


def register_dashboard(
    application: FastAPI,
    get_service: Callable[..., RecommendationService],
    get_principal: Callable[..., Principal],
) -> None:
    Service = Annotated[RecommendationService, Depends(get_service)]
    Viewer = Annotated[Principal, Depends(get_principal)]

    @application.get("/dashboard/static/style.css", include_in_schema=False)
    def style() -> Response:
        return Response(STYLE, media_type="text/css")

    @application.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
    def overview(
        service: Service,
        principal: Viewer,
        view: str = "current",
        bookmaker: Annotated[str | None, Query(max_length=64)] = None,
        decision: Annotated[list[str] | None, Query()] = None,
        limit: int = 50,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
    ) -> HTMLResponse:
        try:
            if view not in ("current", "history"):
                raise ApiError(422, "INVALID_FILTER", "view must be current or history.")
            statuses = frozenset(RecommendationStatus(item) for item in decision or ())
        except ValueError:
            return error_page(ApiError(422, "INVALID_FILTER", "Unknown decision value."))
        except ApiError as error:
            return error_page(error)
        request = RecommendationFilter(
            view="current" if view == "current" else "history",
            bookmaker=bookmaker or None,
            statuses=statuses,
            limit=limit,
            cursor=cursor,
        )
        try:
            result = service.recommendations(principal, request)
            health = service.source_health_for(principal)
        except ApiError as error:
            return error_page(error)
        return page("Recommendations", list_body(result, request, health))

    @application.get(
        "/dashboard/matches/{match_id}", response_class=HTMLResponse, include_in_schema=False
    )
    def match_detail(match_id: UUID, service: Service, principal: Viewer) -> HTMLResponse:
        try:
            result = service.analysis(principal, match_id)
        except ApiError as error:
            return error_page(error)
        first, second = result.match.players
        title = f"{first.display_name} vs {second.display_name}"
        return page(title, analysis_body(result, allowed(principal, Permission.READ_AUDIT)))

    @application.get(
        "/dashboard/audit/{recommendation_id}",
        response_class=HTMLResponse,
        include_in_schema=False,
    )
    def audit_detail(recommendation_id: UUID, service: Service, principal: Viewer) -> HTMLResponse:
        try:
            result = service.audit(principal, recommendation_id)
        except ApiError as error:
            return error_page(error)
        record = result.record
        chain = ", ".join(str(item) for item in result.version_chain)
        rights = "".join(
            f"<li>{escape(item.source_id)}: redistribution "
            f"{'allowed' if item.redistribution_allowed else 'not allowed'}</li>"
            for item in result.source_rights
        )
        dump = json.dumps(
            {
                "record": record.model_dump(mode="json"),
                "context": result.context.model_dump(mode="json"),
            },
            indent=2,
            sort_keys=True,
        )
        body = (
            banner(
                "Internal audit view. The stored record is shown unchanged. Raw source "
                "payloads are not included. Do not redistribute."
            )
            + f"<p>Recorded decision: <strong>{record.status.value}</strong>, version "
            f"{record.version}, decided {timestamp(record.decided_at)}, expires "
            f"{timestamp(record.expires_at)}. Version chain: {escape(chain)}.</p>"
            f'<h2>Source rights</h2><ul class="reasons">{rights}</ul>'
            f"<h2>Stored record and context</h2><pre>{escape(dump)}</pre>"
        )
        return page(f"Audit {record.decision_id}", body)
