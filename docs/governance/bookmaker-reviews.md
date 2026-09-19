# F01.3 — Independent bookmaker review records

Date: 2026-09-19. All three records are **PENDING_REVIEW**. A public page is not
permission for automated collection. No legal or payout approval is represented.

| Bookmaker | Candidate evidence and observed access | Review owner / missing evidence | Runtime state |
|---|---|---|---|
| Betclic | [Official regulations](https://www.betclic.pl/termsandconditions) was readable; it includes rules and communications with downloadable documents | Product/policy reviewer unassigned; permitted collection method, complete applicable tennis rules/communications, signed rights review, retention and effective dates needed | Odds/regulation sources DRAFT and disabled; payout PENDING_REVIEW |
| Superbet | [Official regulations](https://superbet.pl/wiki/regulamin) was reachable, but this is not a complete archive or independent rule review | Separate reviewer unassigned; complete regulations, tennis communications, access rights, retention and applicable dates needed | Odds/regulation sources DRAFT and disabled; payout PENDING_REVIEW |
| Fortuna | [Candidate regulations page](https://www.efortuna.pl/regulaminy) could not be retrieved by the research tool | Separate reviewer unassigned; confirm canonical rules location and supply permitted documents, access rights and applicable dates | Odds/regulation sources DRAFT and disabled; payout PENDING_REVIEW |

Do not label the above observations as archived or approved regulations. The
archive implementation is verified with self-authored synthetic documents. Real
document bytes and approvals remain an external prerequisite, and F01.3's external
review gate is BLOCKED until they exist.

Archive each regulation and communication under a stable document ID and the
scope `payout:<bookmaker>`. Source access terms belong to `source:<source_id>`.
The returned SHA-256 must be included in a new reviewed policy version. All
documents in that scope must be covered: adding a new communication or changing
any document invalidates the previously approved evidence set immediately.
Unchanged bytes are idempotent. Old bytes and versions remain available for audit.

Reviewer checklist: verify publisher and complete document set; record effective
interval and actual review date; confirm tennis format, retirement/walkover,
postponement and void behavior; establish permitted collection/retention and
redistribution; link F06 payout/settlement golden examples; approve only the
explicit scope. Unknown tax or promotion semantics stay disabled. No current
tax rates or bookmaker settlement rules are embedded by this change.
