"""Name keys for candidate generation only (F04.2).

These keys find possible matches. They never prove identity: the resolver must add
non-name corroboration before it accepts a link. Source text stays unchanged elsewhere.
"""

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum

# Letters that Unicode decomposition does not reduce to ASCII.
_TRANSLITERATION = str.maketrans(
    {
        "ł": "l",
        "Ł": "l",
        "ø": "o",
        "Ø": "o",
        "đ": "dj",
        "Đ": "dj",
        "ð": "d",
        "þ": "th",
        "ß": "ss",
        "æ": "ae",
        "Æ": "ae",
        "œ": "oe",
        "Œ": "oe",
        "ı": "i",
    }
)
_SEPARATORS = re.compile(r"[\s\-‐‑‒–—'’`.,]+")
_NON_ALPHA = re.compile(r"[^a-z ]")


def fold(text: str) -> str:
    """Return a lowercase ASCII form with diacritics and punctuation removed."""
    decomposed = unicodedata.normalize("NFKD", text.translate(_TRANSLITERATION))
    stripped = "".join(char for char in decomposed if not unicodedata.combining(char))
    spaced = _SEPARATORS.sub(" ", stripped.casefold())
    return " ".join(_NON_ALPHA.sub("", spaced).split())


@dataclass(frozen=True)
class NameKey:
    tokens: tuple[str, ...]

    @classmethod
    def of(cls, text: str) -> "NameKey":
        return cls(tuple(fold(text).split()))

    @property
    def full(self) -> tuple[str, ...]:
        """Order-free key; it covers ``Surname Given`` and ``Given Surname`` orders."""
        return tuple(sorted(self.tokens))

    @property
    def is_empty(self) -> bool:
        return not self.tokens


class NameMatch(StrEnum):
    EXACT = "EXACT"
    INITIALS = "INITIALS"
    PARTIAL = "PARTIAL"
    NONE = "NONE"


def _initials_compatible(short: tuple[str, ...], full: tuple[str, ...]) -> bool:
    """Match ``N Djokovic`` or ``Djokovic N`` against ``Novak Djokovic``.

    Full tokens must appear in ``full``. Every single-letter token must be the initial of a
    distinct remaining token. At least one full token must match, so bare initials fail.
    """
    remaining = list(full)
    initials = []
    matched_full = 0
    for token in short:
        if len(token) == 1:
            initials.append(token)
        elif token in remaining:
            remaining.remove(token)
            matched_full += 1
        else:
            return False
    if matched_full == 0 or not initials:
        return False
    for initial in initials:
        found = next((item for item in remaining if item.startswith(initial)), None)
        if found is None:
            return False
        remaining.remove(found)
    return True


def compare(left: str, right: str) -> NameMatch:
    a, b = NameKey.of(left), NameKey.of(right)
    if a.is_empty or b.is_empty:
        return NameMatch.NONE
    if a.full == b.full:
        return NameMatch.EXACT
    if _initials_compatible(a.tokens, b.tokens) or _initials_compatible(b.tokens, a.tokens):
        return NameMatch.INITIALS
    short, long = sorted((set(a.tokens), set(b.tokens)), key=len)
    if short < long:
        # "Zverev" or "Carlos Alcaraz" inside a longer name: possibly the same person.
        return NameMatch.PARTIAL
    return NameMatch.NONE


def blocking_keys(text: str) -> frozenset[str]:
    """Index keys: every full token of two or more letters. Used only to find candidates."""
    return frozenset(token for token in NameKey.of(text).tokens if len(token) > 1)
