# CRM Ownership Sync

Bellhaven ↔ CRM reconciliation.

Keeps the parent/child ownership picture of a senior-care operator's facilities
accurate in the CRM. Scrapes the operator's website, links every location to
the right CRM account, proposes fixes with evidence, and writes approved fixes
back through the API. Built for the Clipboard Sales Operations Analyst exercise.

```
website ──scrape──▶ site_locations.json ─┐
                                          ├─match──▶ proposals.json ──review app──▶ CRM (only on approve)
CRM API ──snapshot─▶ crm_accounts.json ──┘                 ▲
                                            decisions.json ┘  (idempotency ledger)
```

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env            # put your CRM token in it
python -m pytest -q             # 45 unit tests, no network
python run_pipeline.py          # scrape + snapshot + match  (never writes to the CRM)
python review_app.py            # http://127.0.0.1:5055
python verify_end_state.py      # after reviewing: prove the CRM matches the decisions
```

Work the queue in the browser: the left rail lists every proposal grouped by change
type, the right pane shows the website record, the CRM record, and what the CRM will
hold after approval, with the changed cells highlighted. **Approve and update CRM**
performs the exact API calls listed on the page and records the decision; **Reject**
just records it. `j`/`k` move through the queue, `a`/`r` decide. Re-running the
pipeline never re-proposes anything already decided.

## What the pipeline proposes

| Type | When | Write |
|---|---|---|
| `CONFIRM_MATCH` | best candidate scores 0.55 to 0.80 | nothing; approve links the pair (fixes follow next run), reject excludes it (a `CREATE` follows) |
| `CREATE` | website location, no CRM account | `POST /accounts` under the parent |
| `REPARENT` | wrong parent, and (no revenue **or** no open AR) | `PATCH parent_id` |
| `CHOW` | wrong parent **and** revenue **and** open AR | `POST` new account under parent, `PATCH old.chow_current_account`; old account otherwise untouched |
| `DUPLICATE` | 2+ accounts resolve to one location | loser: `PATCH duplicate_of_account`, `status=Inactive` |
| `UPDATE_NAME` / `UPDATE_ADDRESS` / `UPDATE_CARE_TYPE` / `UPDATE_PHONE` | same facility, stale field | `PATCH` that field (phone: surviving account only) |
| `REACTIVATE` | Inactive account for a live location | `PATCH status=Active` |
| `NOT_ON_SITE` | under the parent, absent from the website | `PATCH status=Needs Review` + note |
| `CONFIRM` | everything agrees | nothing (listed for transparency) |

Every write appends a dated `[ownership-sync]` line to the account's `note` (except
the CHOW'd old account, which per the SOP gets only the `chow_current_account`
pointer). Phone fixes go to the surviving account only; set `PROPOSE_PHONE_UPDATES=0`
to use phone purely as a matching signal.

## How matching works (`pipeline/match.py`)

Deterministic, no LLM. Each (location, account) pair is scored on independent
identity signals; agreeing signals add, contradicting ones subtract:

| signal | agree | conflict |
|---|---|---|
| normalized street (`Northwest Sylvania Avenue` = `NW Sylvania Ave`, units dropped, PO Box = neutral) | +0.45 | −0.15 |
| zip | +0.20 | −0.05 |
| city + state | +0.15 | −0.10 |
| phone (10 digits) | +0.35 | −0.30, or −0.10 when the street is an exact match |
| name tokens, generic words removed (`Health Care Center` ≈ `Healthcare Centre`) | up to +0.25 | none |

≥ 0.80 is a confident match; 0.55 to 0.80 is **low confidence**, and the pipeline does
not guess: it emits a single `CONFIRM_MATCH` asking the reviewer whether the pair is
the same facility, and only after that link is approved does it propose the field
fixes (rejecting it makes the next run propose a new account instead). Below 0.55 is
not the same facility. A facility with several confident candidates (or weaker
candidates at the exact same street) is a duplicate group. The survivor is ranked:
a CHOW successor first, then open AR, then revenue history, then already under the
right parent, then Active, then a blend of how well the copy agrees with the website
and how complete it is, then lowest id.

Accounts that already carry `chow_current_account` or `duplicate_of_account` are
never re-matched, so the CRM converges after approvals instead of flip-flopping. When
a survivor needs a CHOW, its duplicates are deferred to the run after the CHOW lands
(they need the successor's id).

## Safety

* **Nothing writes without a click.** The pipeline only reads. `POST` is never retried
  (a lost response after the server committed would create the account twice).
* **Validate, then write.** Before the first side effect every PATCH target is re-fetched
  and *every* field in the payload must still hold the value in the proposal's snapshot;
  otherwise nothing is written and the item is reported `stale`.
* **Never create twice.** Every attempt is kept in the ledger; if a change-of-ownership
  created its successor but the link PATCH failed, approving again reuses that account.
* **Idempotent.** A proposal's id is a hash of (type, subject, desired changes, exact write payload).
  `state/decisions.json` records every approve/reject; decided ids are suppressed on
  every later run. An approval whose write failed is *not* treated as decided.
* **One decision at a time.** Approvals are serialized in the app and the buttons
  disable on submit, so a held key or a double click cannot write twice.
* **A bad crawl never becomes a queue.** The pipeline refuses to publish if the scrape
  returns fewer than half the locations of the previous snapshot, and a location whose
  address did not parse never produces an address write.
* **Notes append, never overwrite.** Only API-declared mutable fields are ever sent.

## Running daily

`.github/workflows/daily.yml` runs the pipeline at 06:00 America/Chicago, runs the
tests first, commits the refreshed `state/`, and posts a job summary.
`crontab.example` is the equivalent for a single host. The reviewer opens the app when
they have a minute; decisions persist in `state/decisions.json`. **Commit `state/` after
a review session**: the scheduled run pulls it first, so nothing already decided is
re-proposed.

## Layout

```
pipeline/scrape.py     homepage + paginated directory + detail pages → locations
pipeline/normalize.py  street/phone/name normalization, care-type vocabulary map
pipeline/match.py      scoring, assignment, classification → proposals (pure)
pipeline/ledger.py     decision ledger (idempotency)
pipeline/apply.py      executes an approved proposal (the only writer)
pipeline/crm.py        API client
run_pipeline.py        daily entry point
review_app.py          Flask review UI (templates/)
verify_end_state.py    read-only proof that the CRM matches the recorded decisions
tests/                 normalization, classification, client, apply, scraper, app
state/                 snapshots, proposals, decisions, run log
```
