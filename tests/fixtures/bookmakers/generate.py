"""Generate synthetic SYS-05 golden fixtures for the three bookmaker parsers.

The shapes are invented. They are not real Betclic, Superbet or Fortuna payloads, and
must be replaced by approved samples before any source is enabled (F05.1). The expected
file comes from these scenarios, not from the parsers under test.

Run: uv run python tests/fixtures/bookmakers/generate.py
"""

import json
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


def write(name, payloads, comma_decimal=False):
    folder = ROOT / name
    folder.mkdir(exist_ok=True)
    for poll in (1, 2):
        events = scenarios(poll)
        (folder / f"payload-{poll}.json").write_text(
            json.dumps(payloads(events), ensure_ascii=False, indent=1) + "\n", "utf-8"
        )
        (folder / f"expected-{poll}.json").write_text(
            json.dumps(expected(events, comma_decimal=comma_decimal), indent=1) + "\n", "utf-8"
        )


if __name__ == "__main__":
    write("betclic", betclic)
