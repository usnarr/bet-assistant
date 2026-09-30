"""F15.3 read-only Grafana dashboard, generated so its queries stay in the repository.

The dashboard is provisioned from `deploy/grafana/dashboards/tennis-operations.json`.
Grafana cannot save a change to it (`allowUiUpdates: false`, `editable: false`). A test
checks that the committed file equals this output and that every query names a metric
that the platform exports.
"""

import json
from typing import Any

DATASOURCE = {"type": "prometheus", "uid": "tennis-prometheus"}
DASHBOARD_UID = "tennis-operations"


def _target(expr: str, legend: str = "", instant: bool = False) -> dict[str, Any]:
    target: dict[str, Any] = {"datasource": DATASOURCE, "expr": expr, "refId": "A"}
    if legend:
        target["legendFormat"] = legend
    if instant:
        target |= {"instant": True, "range": False, "format": "table"}
    return target


def _panel(
    panel_id: int,
    title: str,
    kind: str,
    grid: tuple[int, int, int, int],
    target: dict[str, Any],
    description: str,
    unit: str = "",
) -> dict[str, Any]:
    x, y, w, h = grid
    panel: dict[str, Any] = {
        "id": panel_id,
        "type": kind,
        "title": title,
        "description": description,
        "datasource": DATASOURCE,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": [target],
        "fieldConfig": {"defaults": {"unit": unit} if unit else {}, "overrides": []},
        "options": {},
    }
    return panel


PANELS: tuple[tuple[str, str, tuple[int, int, int, int], dict[str, Any], str, str], ...] = (
    (
        "Recommendations allowed",
        "stat",
        (0, 0, 6, 4),
        _target("min(tennis_recommendations_allowed) or vector(0)"),
        "1 when the responsible-use policy and the global stop allow recommendations. "
        "No sample counts as 0.",
        "none",
    ),
    (
        "Firing alerts",
        "stat",
        (6, 0, 6, 4),
        _target('count(ALERTS{alertstate="firing"}) or vector(0)'),
        "Prometheus alerts in the firing state.",
        "none",
    ),
    (
        "Seconds since the last scheduler tick",
        "stat",
        (12, 0, 6, 4),
        _target("time() - tennis_scheduler_last_tick_timestamp_seconds"),
        "More than 300 s raises TennisSchedulerStale.",
        "s",
    ),
    (
        "Scrape targets up",
        "stat",
        (18, 0, 6, 4),
        _target("sum(up) / count(up)"),
        "Share of scrape targets that answered the last scrape.",
        "percentunit",
    ),
    (
        "Firing alerts",
        "table",
        (0, 4, 24, 8),
        _target('ALERTS{alertstate="firing"}', instant=True),
        "Every firing alert with its rule, reason, control and scope.",
        "",
    ),
    (
        "Deterministic alerts (scheduler)",
        "table",
        (0, 12, 12, 8),
        _target("tennis_deterministic_alert", instant=True),
        "The scheduler's own rule evaluation. It applies the stops.",
        "",
    ),
    (
        "Signal values",
        "table",
        (12, 12, 12, 8),
        _target("tennis_signal_value", instant=True),
        "Latest value of each measured signal. A missing signal has no row.",
        "",
    ),
    (
        "Source status",
        "table",
        (0, 20, 12, 8),
        _target("tennis_source_status == 1", instant=True),
        "F14 source-health status per source.",
        "",
    ),
    (
        "Quote observation age",
        "timeseries",
        (12, 20, 12, 8),
        _target("tennis_source_observation_age_seconds", "{{source_id}}"),
        "Age of the latest quote observation per source.",
        "s",
    ),
    (
        "Job outcomes (15 min)",
        "timeseries",
        (0, 28, 12, 8),
        _target("sum by (state) (increase(tennis_job_outcomes_total[15m]))", "{{state}}"),
        "Scheduler job outcomes by state.",
        "none",
    ),
    (
        "HTTP requests",
        "timeseries",
        (12, 28, 12, 8),
        _target(
            "sum by (route, status) (rate(tennis_http_requests_total[5m]))",
            "{{route}} {{status}}",
        ),
        "API requests per second by route template and status.",
        "reqps",
    ),
)


def dashboard() -> dict[str, Any]:
    return {
        "uid": DASHBOARD_UID,
        "title": "Tennis operations",
        "description": "F15 read-only operations dashboard. Synthetic data until real "
        "sources are approved.",
        "tags": ["tennis", "f15"],
        "editable": False,
        "timezone": "utc",
        "refresh": "1m",
        "schemaVersion": 39,
        "version": 1,
        "time": {"from": "now-6h", "to": "now"},
        "panels": [
            _panel(index, title, kind, grid, target, description, unit)
            for index, (title, kind, grid, target, description, unit) in enumerate(PANELS, 1)
        ],
    }


def render_dashboard() -> str:
    return json.dumps(dashboard(), indent=2, sort_keys=True) + "\n"


def expressions() -> tuple[str, ...]:
    return tuple(target["expr"] for *_, target, _, _ in PANELS)
