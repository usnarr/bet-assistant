"""Typed failures safe to cross service boundaries."""

from dataclasses import dataclass

from .contracts import ReasonCode


@dataclass(frozen=True)
class EngineError(Exception):
    reason: ReasonCode
    message: str
    retryable: bool = False

    def __str__(self) -> str:
        return f"{self.reason}: {self.message}"
