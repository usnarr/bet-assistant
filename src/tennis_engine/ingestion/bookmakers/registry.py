"""Configured bookmaker adapters. Each one stays disabled until F01 approves its source."""

from .adapter import BookmakerAdapter
from .betclic import BetclicParser

BETCLIC = BookmakerAdapter(
    bookmaker="betclic",
    source_id="betclic-odds",
    parser=BetclicParser(),
    payout_rule_version="betclic-payout-draft-2026-09-29",
    settlement_rule_version="betclic-settlement-draft-2026-09-29",
)

ADAPTERS: dict[str, BookmakerAdapter] = {adapter.bookmaker: adapter for adapter in (BETCLIC,)}
