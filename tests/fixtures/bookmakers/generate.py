"""Generate synthetic SYS-05 golden fixtures for the three bookmaker parsers.

The shapes are invented. They are not real Betclic, Superbet or Fortuna payloads, and
must be replaced by approved samples before any source is enabled (F05.1). The expected
file comes from these scenarios, not from the parsers under test.

Run: uv run python tests/fixtures/bookmakers/generate.py
"""

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).parent
WARSAW = ZoneInfo("Europe/Warsaw")
BASE = datetime(2026, 9, 21, 10, 0, tzinfo=UTC)
PLAYERS = [
    ("p-01", "Jan Kowalski", "Kowalski J."),
    ("p-02", "Lukasz Wojcik", "Wojcik L."),
    ("p-03", "Anna Nowak", "Nowak A."),
    ("p-04", "Maria Zielinska", "Zielinska M."),
    ("p-05", "Piotr Lewandowski", "Lewandowski P."),
    ("p-06", "Tomasz Kaminski", "Kaminski T."),
    ("p-07", "Ewa Wisniewska", "Wisniewska E."),
    ("p-08", "Karolina Dabrowska", "Dabrowska K."),
]


def selection(sid, side, odds, status="OPEN", promo=False, label=None):
    return {"id": sid, "side": side, "odds": odds, "status": status, "promo": promo, "label": label}


def winner(eid, odds=("1.85", "1.95"), status="OPEN", statuses=("OPEN", "OPEN"), promo=(0, 0)):
    return {
        "id": f"{eid}-mw",
        "kind": "MW",
        "status": status,
        "selections": [
            selection(f"{eid}-s1", 0, odds[0], statuses[0], bool(promo[0])),
            selection(f"{eid}-s2", 1, odds[1], statuses[1], bool(promo[1])),
        ],
    }


def event(index, players, tour, **overrides):
    eid = f"ev-{index:02d}"
    data = {
        "id": eid,
        "tour": tour,
        "competition": "Synthetic Open",
        "start": BASE + timedelta(hours=index),
        "players": [PLAYERS[players[0]], PLAYERS[players[1]]],
        "doubles": False,
        "best_of": 3,
        "state": "PRE_MATCH",
        "markets": [winner(eid)],
        "reject_event": None,  # Expected record-level rejection reason category.
    }
    data.update(overrides)
    return data


def scenarios(poll):
    later = poll == 2
    events = [
        event(1, (0, 1), "ATP", markets=[winner("ev-01", ("1.62", "2.30"))]),
        event(2, (2, 3), "WTA", markets=[winner("ev-02", ("2.05", "1.75"))]),
        # Poll 2 moves the start by one hour with unchanged prices.
        event(3, (4, 5), "ATP", start=BASE + timedelta(hours=3 + (1 if later else 0))),
        event(4, (6, 7), "WTA", markets=[winner("ev-04", ("1.40", "2.95" if later else "2.90"))]),
        event(5, (0, 2), "ATP"),
        event(6, (1, 3), "WTA"),
        event(7, (4, 6), "ATP"),
        event(8, (5, 7), "WTA"),
        event(9, (1, 0), "ATP", markets=[winner("ev-09", ("2.30", "1.62"))]),  # Reversed.
        event(10, (2, 4), "ATP", markets=[winner("ev-10", statuses=("SUSPENDED", "OPEN"))]),
        event(11, (3, 5), "WTA", markets=[winner("ev-11", status="SUSPENDED")]),
        event(12, (6, 0), "ATP", markets=[winner("ev-12", ("1,85", "1.95"))]),  # Malformed.
        event(13, (7, 1), "WTA", markets=[winner("ev-13", ("1.00", "7.50"))]),  # Not above 1.
        event(14, (0, 3), "ATP", start=None, reject_event="missing-start"),
        event(
            15,
            (2, 5),
            "ATP",
            markets=[
                winner("ev-15"),
                {
                    "id": "ev-15-tg",
                    "kind": "TOTAL",
                    "status": "OPEN",
                    "selections": [
                        selection("ev-15-o", None, "1.90", label="Over 22.5"),
                        selection("ev-15-u", None, "1.90", label="Under 22.5"),
                    ],
                },
            ],
        ),
        event(16, (4, 7), "ATP", doubles=True),
        event(17, (1, 6), "ATP", best_of=5),
        event(18, (3, 7), "WTA", state="STARTED"),
        event(19, (0, 5), "ATP", state="CANCELLED"),
        event(20, (2, 6), "WTA", markets=[winner("ev-20", status="CLOSED")]),
        event(21, (5, 1), "ATP", markets=[winner("ev-21", ("2.10", "1.72"), promo=(1, 0))]),
        event(22, (6, 3), "WTA", reject_event="unknown-timezone"),
    ]
    return events


def effective_state(market, item):
    if market["status"] != "OPEN":
        return market["status"]
    return item["status"]


def valid_odds(value):
    parts = value.split(".")
    return len(parts) == 2 and all(part.isdigit() for part in parts) and float(value) > 1


def expected(events, *, comma_decimal):
    result = {"events": {}, "quotes": {}, "rejected_selections": [], "rejected_events": []}
    for item in events:
        if item["reject_event"]:
            result["rejected_events"].append(item["id"])
            continue
        result["events"][item["id"]] = {
            "start": item["start"].isoformat(),
            "state": item["state"],
            "participants": [player[1] for player in item["players"]],
            "doubles": item["doubles"],
            "best_of": item["best_of"],
            "tour": item["tour"],
        }
        for market in item["markets"]:
            for sel in market["selections"]:
                odds = sel["odds"]
                if comma_decimal and odds == "1,85":
                    odds = "1.85"  # A comma is Fortuna's normal separator in this shape.
                if not valid_odds(odds):
                    result["rejected_selections"].append(sel["id"])
                    continue
                result["quotes"][sel["id"]] = {
                    "odds": odds,
                    "state": effective_state(market, sel),
                    "participant_index": sel["side"] if market["kind"] == "MW" else None,
                    "supported": market["kind"] == "MW",
                    "promotion": sel["promo"],
                }
    return result


# --- Betclic synthetic shape: ISO starts with offset, contestants 1/2, text statuses.
BETCLIC_STATUS = {"OPEN": "Open", "SUSPENDED": "Suspended", "CLOSED": "Closed"}
BETCLIC_MARKET = {"MW": "Zwycięzca meczu", "TOTAL": "Liczba gemów"}


def betclic(events):
    matches = []
    for item in events:
        start = item["start"]
        if item["reject_event"] == "unknown-timezone":
            date = start.replace(tzinfo=None).isoformat()
        elif start is None:
            date = None
        else:
            date = start.astimezone(WARSAW).isoformat()
        match = {
            "id": item["id"],
            "competition": {
                "name": item["competition"],
                "gender": "M" if item["tour"] == "ATP" else "F",
            },
            "live": item["state"] == "STARTED",
            "cancelled": item["state"] == "CANCELLED",
            "doubles": item["doubles"],
            "bestOf": item["best_of"],
            "contestants": [{"id": f"bc-{p[0]}", "name": p[1]} for p in item["players"]],
            "markets": [
                {
                    "id": market["id"],
                    "name": BETCLIC_MARKET[market["kind"]],
                    "status": BETCLIC_STATUS[market["status"]],
                    "selections": [
                        {
                            "id": sel["id"],
                            "name": sel["label"] or item["players"][sel["side"]][1],
                            "contestant": None if sel["side"] is None else sel["side"] + 1,
                            "odds": sel["odds"],
                            "status": BETCLIC_STATUS[sel["status"]],
                            "boosted": sel["promo"],
                        }
                        for sel in market["selections"]
                    ],
                }
                for market in item["markets"]
            ],
        }
        if date is not None:
            match["date"] = date
        matches.append(match)
    return {"format": "betclic-synthetic-v1", "matches": matches}


# --- Superbet synthetic shape: epoch-ms UTC starts, one "A - B" name, numeric statuses,
# JSON-number prices. A malformed price appears as a string and must be rejected.
SUPERBET_STATUS = {"OPEN": 1, "SUSPENDED": 2, "CLOSED": 3}
SUPERBET_EVENT = {"PRE_MATCH": 0, "STARTED": 1, "CANCELLED": 9}
SUPERBET_MARKET = {"MW": "Zwycięzca", "TOTAL": "Liczba gemów"}


class Number(str):
    """Marks odds text that must be written as a bare JSON number."""


def superbet(events):
    data = []
    for item in events:
        names = [p[1] for p in item["players"]]
        entry = {
            "matchId": item["id"],
            "matchName": f"{names[0]} - {names[1]}",
            "tournamentName": item["competition"],
            "sportCategory": f"{item['tour']} {'Doubles' if item['doubles'] else 'Singles'}",
            "bestOfSets": item["best_of"],
            "status": SUPERBET_EVENT[item["state"]],
            "odds": [],
        }
        if item["reject_event"] == "unknown-timezone":
            entry["matchTimestamp"] = item["start"].replace(tzinfo=None).isoformat()
        elif item["start"] is not None:
            entry["matchTimestamp"] = int(item["start"].timestamp() * 1000)
        for market in item["markets"]:
            for sel in market["selections"]:
                odds = sel["odds"]
                entry["odds"].append(
                    {
                        "marketId": market["id"],
                        "marketName": SUPERBET_MARKET[market["kind"]],
                        "marketStatus": SUPERBET_STATUS[market["status"]],
                        "outcomeId": sel["id"],
                        "outcomeName": sel["label"] or str(sel["side"] + 1),
                        "price": Number(odds) if valid_number(odds) else odds,
                        "status": SUPERBET_STATUS[sel["status"]],
                        "boost": sel["promo"],
                    }
                )
        data.append(entry)
    return {"schema": "superbet-synthetic-v1", "data": data}


def valid_number(text):
    try:
        float(text)
    except ValueError:
        return False
    return True


# --- Fortuna synthetic shape: naive Europe/Warsaw local starts, comma decimal odds,
# participants listed away first with explicit home/away positions, boolean states.
FORTUNA_EVENT = {"PRE_MATCH": "NOT_STARTED", "STARTED": "IN_PLAY", "CANCELLED": "CANCELLED"}
FORTUNA_MARKET = {"MW": "MATCH_RESULT", "TOTAL": "TOTAL_GAMES"}
POSITION = {0: "home", 1: "away", None: None}


def fortuna_flags(status, *, market):
    if market:
        return {"open": status != "CLOSED", "suspended": status == "SUSPENDED"}
    return {"active": status != "CLOSED", "suspended": status == "SUSPENDED"}


def fortuna(events):
    competitions = {}
    for item in events:
        entry = {
            "code": item["id"],
            "state": FORTUNA_EVENT[item["state"]],
            "format": None if item["best_of"] is None else f"BO{item['best_of']}",
            "pair": item["doubles"],
            # Away is listed first on purpose; position, not list order, decides sides.
            "participants": [
                {
                    "position": "away",
                    "id": f"fo-{item['players'][1][0]}",
                    "name": item["players"][1][1],
                },
                {
                    "position": "home",
                    "id": f"fo-{item['players'][0][0]}",
                    "name": item["players"][0][1],
                },
            ],
            "markets": [
                {
                    "code": market["id"],
                    "type": FORTUNA_MARKET[market["kind"]],
                    **fortuna_flags(market["status"], market=True),
                    "outcomes": [
                        {
                            "code": sel["id"],
                            "label": sel["label"] or item["players"][sel["side"]][1],
                            "position": POSITION[sel["side"]],
                            "odds": sel["odds"].replace(".", ","),
                            **fortuna_flags(sel["status"], market=False),
                            "superOdds": sel["promo"],
                        }
                        for sel in market["selections"]
                    ],
                }
                for market in item["markets"]
            ],
        }
        if item["reject_event"] == "unknown-timezone":
            entry["startLocal"] = "2026-10-25 02:30"  # Occurs twice in Warsaw (DST end).
        elif item["start"] is not None:
            entry["startLocal"] = item["start"].astimezone(WARSAW).strftime("%Y-%m-%d %H:%M")
        competitions.setdefault((item["competition"], item["tour"]), []).append(entry)
    return {
        "version": "fortuna-synthetic-v1",
        "sports": [
            {
                "sport": "tennis",
                "competitions": [
                    {"name": name, "category": tour, "events": entries}
                    for (name, tour), entries in competitions.items()
                ],
            }
        ],
    }


def write(name, payloads, comma_decimal=False):
    folder = ROOT / name
    folder.mkdir(exist_ok=True)
    for poll in (1, 2):
        events = scenarios(poll)
        (folder / f"payload-{poll}.json").write_text(encode(payloads(events)) + "\n", "utf-8")
        (folder / f"expected-{poll}.json").write_text(
            json.dumps(expected(events, comma_decimal=comma_decimal), indent=1) + "\n", "utf-8"
        )


def encode(value):
    """JSON text in which `Number` strings are written as bare numbers, keeping their text."""

    def mark(item):
        if isinstance(item, Number):
            return f"__NUMBER__{item}__"
        if isinstance(item, dict):
            return {key: mark(inner) for key, inner in item.items()}
        if isinstance(item, list):
            return [mark(inner) for inner in item]
        return item

    text = json.dumps(mark(value), ensure_ascii=False, indent=1)
    return re.sub(r'"__NUMBER__([0-9.]+)__"', lambda found: found.group(1), text)


if __name__ == "__main__":
    write("betclic", betclic)
    write("superbet", superbet)
    write("fortuna", fortuna, comma_decimal=True)
