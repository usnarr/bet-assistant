"""Versioned parser boundary; source-specific parsers arrive in F04/F05."""

from typing import Protocol

from .contracts import ParsedItem


class SourceParser(Protocol):
    @property
    def version(self) -> str: ...

    def parse(self, body: bytes) -> tuple[ParsedItem, ...]: ...


class ParserRegistry:
    def __init__(self, parsers: tuple[SourceParser, ...] = ()) -> None:
        self._parsers: dict[str, SourceParser] = {}
        for parser in parsers:
            self.register(parser)

    def register(self, parser: SourceParser) -> None:
        if parser.version in self._parsers:
            raise ValueError(f"Duplicate parser version {parser.version!r}")
        self._parsers[parser.version] = parser

    def get(self, version: str) -> SourceParser:
        try:
            return self._parsers[version]
        except KeyError as error:
            raise ValueError(f"Unknown parser version {version!r}") from error
