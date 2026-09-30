"""F15.3 metrics endpoint for processes without FastAPI (scheduler, egress proxy).

The server uses the standard library only. It serves:

- `GET /metrics`: the registry in the Prometheus text format.
- `GET /health`: 200 when the health callback returns True, else 503.
- `POST /alertmanager`: an Alertmanager webhook, when an alert sink is set.

It runs on an internal Docker network and publishes no host port. A request body is
limited to 1 MiB. The access log is off, so a request line never reaches the logs.
"""

import json
import logging
import threading
from collections.abc import Callable, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .metrics import MetricsRegistry, label_value

MAX_BODY_BYTES = 1 << 20
CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
logger = logging.getLogger("tennis_engine.monitoring.exporter")

AlertSink = Callable[[Mapping[str, Any]], None]


class AlertNotifications:
    """Counts Alertmanager notifications and logs codes only, never values or annotations."""

    LABELS = ("alertname", "severity", "rule_id", "scope", "reason")

    def __init__(self, registry: MetricsRegistry) -> None:
        self.received = registry.counter(
            "tennis_alert_notifications_total",
            "Alertmanager notifications received, by alert name, severity and status.",
            ("alertname", "severity", "status"),
        )

    def __call__(self, payload: Mapping[str, Any]) -> None:
        alerts = payload.get("alerts")
        if not isinstance(alerts, list):
            raise ValueError("The webhook payload has no alert list")
        for alert in alerts[:1000]:
            if not isinstance(alert, Mapping):
                continue
            labels = alert.get("labels")
            labels = labels if isinstance(labels, Mapping) else {}
            status = label_value(alert.get("status", "unknown"))
            self.received.inc(
                alertname=labels.get("alertname", "unknown"),
                severity=labels.get("severity", "unknown"),
                status=status,
            )
            logger.warning(
                "alert notification",
                extra={
                    "context": {
                        "status": status,
                        **{name: label_value(labels.get(name, "")) for name in self.LABELS},
                    }
                },
            )


class MetricsServer:
    def __init__(
        self,
        registry: MetricsRegistry,
        host: str,
        port: int,
        *,
        health: Callable[[], bool] = lambda: True,
        alert_sink: AlertSink | None = None,
    ) -> None:
        self.registry = registry
        handler = _handler(registry, health, alert_sink)
        self._server = ThreadingHTTPServer((host, port), handler)
        self._server.daemon_threads = True
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="metrics-server", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        # `shutdown` waits for `serve_forever`, so call it only after a start.
        if self._thread is not None:
            self._server.shutdown()
            self._thread.join(timeout=5)
        self._server.server_close()


def _handler(
    registry: MetricsRegistry, health: Callable[[], bool], alert_sink: AlertSink | None
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "tennis-exporter"
        sys_version = ""

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

        def _reply(self, status: int, body: bytes, content_type: str = "text/plain") -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - http.server API
            if self.path == "/metrics":
                self._reply(200, registry.render().encode(), CONTENT_TYPE)
            elif self.path == "/health":
                try:
                    healthy = health()
                except Exception:  # noqa: BLE001 - a failed check is unhealthy
                    healthy = False
                self._reply(200 if healthy else 503, b"ok\n" if healthy else b"unhealthy\n")
            else:
                self._reply(404, b"not found\n")

        def do_POST(self) -> None:  # noqa: N802 - http.server API
            if self.path != "/alertmanager" or alert_sink is None:
                self._reply(404, b"not found\n")
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = -1
            if not 0 < length <= MAX_BODY_BYTES:
                self._reply(413 if length > MAX_BODY_BYTES else 400, b"invalid length\n")
                return
            try:
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, Mapping):
                    raise ValueError("The payload is not an object")
                alert_sink(payload)
            except (ValueError, TypeError):
                self._reply(400, b"invalid payload\n")
                return
            self._reply(200, b"ok\n")

    return Handler
