# F01.2 — Provider comparison and evidence gaps

Prepared 2026-09-19 from the official documentation linked below. These are vendor
documentation observations, not trial results, rights approvals or endorsements.
No authenticated API requests, purchases or provider contacts were made.

| Candidate | Coverage, history and stats evidence | Identity/corrections evidence | Latency, support, rights and cost gaps |
|---|---|---|---|
| [Sportradar](https://developer.sportradar.com/tennis/reference/overview) | Tennis scoring, rankings, statistics and point timelines where available; documented competition-season endpoints expose at most three seasons | Competition/player/event identifiers, competitor merge mappings and created/updated/removed event feeds are documented | Push delivery is documented; measured latency, contracted history, completeness, SLA, price and permitted retention/commercial redistribution are unverified |
| [API-Tennis](https://api-tennis.com/documentation) | Subscription-specific events/tournaments; fixtures contain scores, point-by-point and statistics when available | Examples include event, tournament and player keys; cross-season ID stability and correction policy require a trial | No measured delay, historical completeness, support/SLA, approved price or license evidence in this review |
| [Matchstat](https://tennisapidoc.matchstat.com/) | Documents ATP/WTA coverage, service/return statistics and historical odds as far back as 2010 | Numeric core player/fixture IDs; live event IDs differ from core fixtures, requiring mapping validation | WebSocket updates depend on plan; docs state a global 100 requests/minute/IP ceiling plus plan limits. Contractual rights, correction guarantees, latency, support and price remain unverified |
| [The Odds API](https://the-odds-api.com/sports/tennis-odds.html) | Documents Grand Slams, ATP/WTA 1000/500; mainly match winner with selected spreads/totals. Paid historical coverage varies by tournament and begins as early as 2020 | Response examples have event IDs and bookmaker update timestamps; player identity and correction guarantees need evaluation | Current odds have a free tier; historical data requires paid access. Target Polish bookmaker coverage, executable odds, exact costs, permitted use and SLA are unverified |

No candidate is scored numerically yet: doing so would turn unknowns into apparent
evidence. The source §9 weights remain coverage 15%, historical depth 15%, point
data 10%, match stats 10%, stable IDs 10%, odds 10%, latency 10%, corrections 5%,
SLA/support 5%, commercial rights 10%. Cost is a separate procurement constraint.

Before selection, data engineering must obtain permitted trial samples and measure
ATP/WTA best-of-three coverage, historical cutoffs, missing service counts, ID
stability, corrections and timestamp/latency behavior. Product/policy must record
contract rights, permitted purposes, quota/concurrency, raw retention/deletion,
redistribution restrictions, support and a dated written price quote. Unknowns
remain unknown until evidence is archived and a named reviewer approves it.

Secondary research, official ranking, regulation and forecast sources are listed
in `configs/governance/sources/`. Their links are review candidates inherited from
the blueprint; this implementation has not certified their current access rights.
