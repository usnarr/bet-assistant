"""F15.3 in-process metrics with the Prometheus text exposition format.

No dependency is needed: the registry holds counters, gauges and histograms, and renders
text on each scrape. Label values are bounded and sanitized, so a metric cannot carry a
token, a URL query or a free-text payload. A collector that fails at scrape time reports
`tennis_metrics_collector_up 0`. Missing telemetry is never shown as healthy.
"""

import re
import threading
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Literal

Kind = Literal["counter", "gauge", "histogram"]
Labels = tuple[tuple[str, str], ...]

NAME = re.compile(r"^[a-z_][a-z0-9_]*$")
UNSAFE_LABEL_CHARACTERS = re.compile(r"[^A-Za-z0-9_.:/{}\-]")
MAX_LABEL_LENGTH = 96
DEFAULT_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)


def label_value(value: object) -> str:
    """A bounded label value. Unsafe characters become `_`."""
    text = UNSAFE_LABEL_CHARACTERS.sub("_", str(value))
    return text[:MAX_LABEL_LENGTH] or "_"


def _labels(names: Sequence[str], values: dict[str, object]) -> Labels:
    if set(values) != set(names):
        raise ValueError(f"Expected labels {sorted(names)}, got {sorted(values)}")
    return tuple((name, label_value(values[name])) for name in names)


def _render_labels(labels: Labels, extra: Labels = ()) -> str:
    items = labels + extra
    if not items:
        return ""
    return "{" + ",".join(f'{name}="{value}"' for name, value in items) + "}"


def _number(value: float) -> str:
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return repr(value)


@dataclass(frozen=True)
class Sample:
    """One value from a scrape-time collector."""

    name: str
    value: float
    labels: Labels = ()


@dataclass(frozen=True)
class Family:
    name: str
    kind: Kind
    help: str
    label_names: tuple[str, ...]


class Counter:
    def __init__(self, registry: "MetricsRegistry", family: Family) -> None:
        self._registry = registry
        self.family = family

    def inc(self, amount: float = 1.0, **labels: object) -> None:
        if amount < 0:
            raise ValueError("A counter cannot decrease")
        key = _labels(self.family.label_names, labels)
        with self._registry.lock:
            values = self._registry.values[self.family.name]
            values[key] = values.get(key, 0.0) + amount


class Gauge:
    def __init__(self, registry: "MetricsRegistry", family: Family) -> None:
        self._registry = registry
        self.family = family

    def set(self, value: float, **labels: object) -> None:
        key = _labels(self.family.label_names, labels)
        with self._registry.lock:
            self._registry.values[self.family.name][key] = float(value)


class Histogram:
    def __init__(
        self, registry: "MetricsRegistry", family: Family, buckets: Sequence[float]
    ) -> None:
        self._registry = registry
        self.family = family
        self.buckets = tuple(sorted(buckets))

    def observe(self, value: float, **labels: object) -> None:
        key = _labels(self.family.label_names, labels)
        with self._registry.lock:
            state = self._registry.histograms[self.family.name].setdefault(
                key, [0.0] * (len(self.buckets) + 2)
            )
            for index, bound in enumerate(self.buckets):
                if value <= bound:
                    state[index] += 1
            state[-2] += 1  # count
            state[-1] += value  # sum


Collector = Callable[[], Iterable[Sample]]


class MetricsRegistry:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.families: dict[str, Family] = {}
        self.values: dict[str, dict[Labels, float]] = {}
        self.histograms: dict[str, dict[Labels, list[float]]] = {}
        self._buckets: dict[str, tuple[float, ...]] = {}
        self._collectors: dict[str, tuple[Collector, tuple[Family, ...]]] = {}
        self._collector_up = self.gauge(
            "tennis_metrics_collector_up",
            "1 when a scrape-time collector ran, 0 when it failed.",
            ("collector",),
        )

    def _family(self, name: str, kind: Kind, help: str, labels: Sequence[str]) -> Family:
        if not NAME.match(name) or any(not NAME.match(label) for label in labels):
            raise ValueError(f"Invalid metric or label name: {name}")
        family = Family(name, kind, help, tuple(labels))
        existing = self.families.get(name)
        if existing is not None:
            if existing != family:
                raise ValueError(f"Metric {name} is already registered differently")
            return existing
        self.families[name] = family
        return family

    def counter(self, name: str, help: str, labels: Sequence[str] = ()) -> Counter:
        family = self._family(name, "counter", help, labels)
        self.values.setdefault(name, {})
        return Counter(self, family)

    def gauge(self, name: str, help: str, labels: Sequence[str] = ()) -> Gauge:
        family = self._family(name, "gauge", help, labels)
        self.values.setdefault(name, {})
        return Gauge(self, family)

    def histogram(
        self,
        name: str,
        help: str,
        labels: Sequence[str] = (),
        buckets: Sequence[float] = DEFAULT_BUCKETS,
    ) -> Histogram:
        family = self._family(name, "histogram", help, labels)
        self.histograms.setdefault(name, {})
        histogram = Histogram(self, family, buckets)
        self._buckets[name] = histogram.buckets
        return histogram

    def register_collector(
        self, collector_id: str, collector: Collector, families: Sequence[Family]
    ) -> None:
        """A collector returns samples at scrape time for the families it declares."""
        for family in families:
            self._family(family.name, family.kind, family.help, family.label_names)
        self._collectors[collector_id] = (collector, tuple(families))

    def value(self, name: str, **labels: object) -> float | None:
        """The current counter or gauge value, for tests and evidence."""
        family = self.families[name]
        key = _labels(family.label_names, labels)
        return self.values.get(name, {}).get(key)

    def render(self) -> str:
        collected: dict[str, list[Sample]] = {}
        for collector_id, (collector, families) in sorted(self._collectors.items()):
            try:
                samples = list(collector())
                names = {family.name for family in families}
                if any(sample.name not in names for sample in samples):
                    raise ValueError("A collector returned an undeclared metric")
            except Exception:  # noqa: BLE001 - a failed collector is reported, not hidden
                self._collector_up.set(0, collector=collector_id)
                continue
            self._collector_up.set(1, collector=collector_id)
            for sample in samples:
                labels = tuple((name, label_value(value)) for name, value in sample.labels)
                collected.setdefault(sample.name, []).append(
                    Sample(sample.name, sample.value, labels)
                )
        lines: list[str] = []
        with self.lock:
            for name in sorted(self.families):
                family = self.families[name]
                lines.append(f"# HELP {name} {family.help}")
                lines.append(f"# TYPE {name} {family.kind}")
                if family.kind == "histogram":
                    buckets = self._buckets[name]
                    for labels, state in sorted(self.histograms[name].items()):
                        for index, bound in enumerate(buckets):
                            extra = (("le", _number(bound)),)
                            lines.append(
                                f"{name}_bucket{_render_labels(labels, extra)} "
                                f"{_number(state[index])}"
                            )
                        extra = (("le", "+Inf"),)
                        lines.append(
                            f"{name}_bucket{_render_labels(labels, extra)} {_number(state[-2])}"
                        )
                        lines.append(f"{name}_count{_render_labels(labels)} {_number(state[-2])}")
                        lines.append(f"{name}_sum{_render_labels(labels)} {_number(state[-1])}")
                    continue
                for labels, value in sorted(self.values.get(name, {}).items()):
                    lines.append(f"{name}{_render_labels(labels)} {_number(value)}")
                for sample in sorted(collected.get(name, []), key=lambda item: item.labels):
                    lines.append(f"{name}{_render_labels(sample.labels)} {_number(sample.value)}")
        return "\n".join(lines) + "\n"
