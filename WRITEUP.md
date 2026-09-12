# Writeup: Bellhaven ownership reconciliation

## What I found in the data before writing any matching code

I spent the first stretch reading the website and the CRM dump side by side rather
than starting on the scraper, because the shape of the mess decides the design.

* The website lists **34** communities in the paginated directory but announces **35**
  on the homepage. The 35th, *Bellhaven Meadows of Findlay*, is linked only from a
  homepage banner. So the scraper crawls the homepage *and* every directory page for
  `/communities/<slug>` links, then fetches each detail page (the directory cards
  only show one care badge; detail pages list all offerings, the full address and
  phone).
* The About page explains most of the ownership drift: Bellhaven **acquired
  Harborview Care Group in 2025 and select Cedar Trail communities in 2026**. That is
  why live Bellhaven locations are parented to Harborview / Cedar Trail in the CRM,
  and why several locations have two or three accounts (one per former owner).
* Every category in the brief is present: rebrands at the same address
  (`Chesterton Senior Commons` → *Bellhaven of Chesterton*), a PO Box where the
  street should be, a zip typo (45626 → 45662), three copies of Monroe, an orphan
  with no parent at all, three accounts under Bellhaven that are not on the site,
  and a **false friend**, `Union Square Senior Living`, in the same town as
  *Bellhaven at Union Square* but at a different street with a different phone.
* Two, and only two, accounts trip the CHOW rule: Tiffin (revenue $84k, AR $12,400)
  and Marietta ($51k, $3,800), both under Cedar Trail. Lima has revenue but zero AR,
  so it re-parents directly. Findlay is the same case with no parent.

## Matching approach

Deterministic and explainable, with no LLM in the pipeline. Each (website location,
CRM account) pair gets a score from independent identity signals: normalized street
(+0.45), zip (+0.20), city/state (+0.15), 10-digit phone (+0.35), and name-token
overlap with generic words like *Health Care Center* stripped (up to +0.25).
Contradictions subtract, and the penalties are the interesting part: a different
phone at the *same* street is cheap (−0.10, stale data is common) but a different
phone at a *different* street is expensive (−0.30) because two independent signals
now disagree. That single rule is what separates Union Square (net 0.07 → create a
new account, with the rejected candidate shown to the reviewer) from Zanesville
(same street, different phone, different name → 0.76, surfaced as a confirm-match
question for a human to answer). PO Boxes are neutral rather than a conflict, which
is how Ashtabula still matches at 0.95 on zip + city + phone + name.

≥ 0.80 is confident. 0.55 to 0.80 is **low confidence**, and there the pipeline asks
rather than guesses: it emits one *confirm match* item ("is Cedar Trail of Zanesville
this building?") with no write behind it. Approving links the pair and the next run
proposes the field fixes; rejecting excludes the pair and the next run proposes a new
account. That keeps the identity decision separate from the field decisions, so they
cannot be approved inconsistently, and rejecting a bad match is never a dead end.
Below 0.55 is not the same facility. Duplicate groups are every confident candidate
plus any weaker candidate at the exact same street (the third Monroe copy fails on
name and phone but the building is the building). Survivor selection is a ranking:
an account that is itself a CHOW successor first (so an approved change of ownership
is never unwound by a later duplicate contest), then open AR, then revenue history,
then already under the right parent, then Active, then a blend of how well the copy
agrees with the website and how complete it is, then lowest id, so the choice is
stable run to run and never orphans a balance.

Accounts that already carry `chow_current_account` or `duplicate_of_account` are
excluded from matching entirely. That is what makes the system converge: after a
CHOW is approved, the next run sees the new account, matches it, and leaves the
frozen old one alone instead of proposing it as a duplicate of its own successor.

## Handling the SOP

`REPARENT` vs `CHOW` is decided per account: wrong parent **and** `lifetime_revenue > 0`
**and** `outstanding_ar > 0` → CHOW; otherwise direct re-parent. A CHOW is two API
calls in order: `POST` a new account built from the website record (name, address,
primary care type, phone, and a note explaining the CHOW and the preserved billing
figures), then `PATCH` the old account with *only* `chow_current_account`. I
deliberately do not add a note to the old account: the SOP says leave it exactly as
it is, and the explanation lives on the new account instead. No name/address fixes
are proposed on a CHOW'd account for the same reason.

## Choices worth explaining

* **Not-on-site → `Needs Review`, not `Inactive`.** Absence from a marketing site is
  weak evidence; it could be a scrape gap, a community mid-transition, or just
  unlisted. Alliance and Coldwater are empty shells, but Sandusky has $130k revenue
  and $5,200 open AR; deactivating that on a website diff would be wrong. All three
  get a dated note; the one with billing exposure says so explicitly.
* **Duplicates.** Loser gets `duplicate_of_account` = survivor and `status=Inactive`,
  with a note naming the survivor and the reason. No losing copy in this dataset has
  billing history; if one did, the survivor rule would have kept it instead, and the
  item carries a warning when more than one copy has history. When the survivor itself
  needs a CHOW, its duplicates wait one run, because they have to point at the successor, which
  does not exist until the CHOW is approved.
* **Phone.** I started with phone as a matching signal only (it is what catches
  Ashtabula's PO Box and Chesterton's rebrand) and did not propose phone updates,
  on the theory that a CRM phone might be a rep's contact rather than the switchboard.
  Reviewing the queue changed my mind: the CRM numbers were uniformly stale, and the
  number on a community's own page is the one the operator publishes. Phone fixes are
  proposed now, on the surviving account only, and the flag to turn them off is still
  there (`PROPOSE_PHONE_UPDATES=0`). The "already matches" wording names exactly
  which fields were compared so it never overclaims.
* **Care type and address spelling.** The site's marketing labels map to the CRM
  vocabulary (*Short-Term Rehabilitation & Nursing* → Skilled Nursing, *Memory Support*
  → Memory Care). A change is only proposed when the CRM value is empty or not among
  the offerings at all; Erie lists Assisted Living and Memory Support, the CRM says
  Assisted Living, and that is not wrong. Likewise fifteen accounts spell the street
  differently from the website ("College Avenue" vs "College Ave", in both directions);
  the normalizer treats those as the same address and I do not rewrite a correct
  address for spelling. Writes happen when the data is wrong (a PO Box, a zip typo, a
  stale name), not when it is merely formatted differently.
* **Notes append; nothing overwrites.** Every write adds one dated
  `[ownership-sync]` line so a rep can see what the bot did and why.

## Safety and re-runs

The pipeline never writes. Approvals happen in the review app, which performs the
exact API calls listed on the page. Each proposal's id is a hash of (type, subject,
desired changes, exact write payload), so a page left open across a re-run cannot
approve values the reviewer never saw; `state/decisions.json` records every approve and reject, and
decided ids are suppressed on every later run; a second run right after a review
produces an empty queue. An approval whose write failed is not counted as decided,
so it comes back; but every attempt is kept, so if a change of ownership created its
successor and then the link PATCH failed, approving again reuses that account rather
than creating a second one. Writes are validate-then-execute: every PATCH target is
re-fetched and every payload field must still match the snapshot before the first
side effect. `POST` is never retried. Decisions are serialized in the app, and the
pipeline refuses to publish a queue from a crawl that came back suspiciously small.

The daily schedule is a GitHub Actions workflow (06:00 Chicago, tests first, commits
the refreshed queue, posts a job summary) with an equivalent crontab line.

## How I used AI tools

I ran this in Claude Code and treated it as a pair: I directed, it typed, I checked.
Concretely:

* I had it dump the website and the full CRM before designing anything, and made it
  show me the raw recon (address collisions, parent distribution, orphans) so the
  design was based on what was actually there.
* The judgment calls were mine: Needs Review over Inactive for the not-on-site
  accounts, phone as signal-only at first and then turning phone fixes on once I had
  looked at the data, and that I would approve every proposal by hand in the app
  rather than have the assistant bulk-approve.
* I checked its work where it was most likely to be wrong. The first matcher pass
  let the third Monroe copy escape the
  duplicate group (fixed with the same-street rule), and picked the wrong Owosso
  survivor; the copy whose phone actually matched the website was going to be
  marked Inactive. That is why "agreement with the website" is in the survivor
  ranking. It also initially hid the Union Square near-miss because the *net* score
  was low; the reviewer needs to see what was considered, so near-misses now rank on
  positive signals.
* When the build was done I ran a multi-agent code review over the whole repo
  (ten finders, one verifier per candidate, a gap sweep). It confirmed fifteen real
  defects I would not have found by reading: the CHOW two-step could create a second
  successor if the link PATCH failed after the POST; `POST` was retried on a 5xx; a
  read-side timeout escaped both exception handlers; two overlapping approvals could
  both write; a third copy of the Kettering building at "3313 Wilmington **Pk**" was
  slipping past the street normalizer; and a low-confidence match that the reviewer
  rejected had no path to a new account. Every one has a regression test now (45
  tests, no network).

## What actually landed

I reviewed every item in the app by hand. There were 49 decisions, all approved, none failed:
3 confirm-match links, 2 changes of ownership (Tiffin and Marietta got successor
accounts; the old accounts kept their revenue and AR untouched), 4 moves under
Bellhaven, 7 duplicates retired, 4 accounts created, 9 renames, 2 address fixes,
15 phone fixes, and 3 accounts flagged Needs Review. A full pipeline run afterward
(fresh scrape, fresh CRM pull) returns 0 to review with all 35 website locations
matched. `verify_end_state.py` then checks the CRM directly: every applied write
holds, both CHOW old accounts are frozen, every website location resolves to exactly
one Active account under Bellhaven with the same name, zip and phone, no two Active
Bellhaven accounts share an address, and all 7 retired duplicates are Inactive and
point at an Active survivor. 127 accounts: 121 + 2 successors + 4 created.

## What I'd build next

1. **Contacts.** Every community page names an administrator (Findlay's is Sam
   Pruitt), which is the person a rep actually calls. The CRM has a contacts table
   the API can write to, and the pipeline ignores that field today. Proposing "add or
   update the administrator contact on this account" is the same approve/reject flow
   as a phone fix.
2. **Stronger evidence before deactivating.** When a community disappears from the
   website, all the pipeline can honestly say is "someone should look." Nursing homes
   are state licensed and Medicare publishes their owners. If the pipeline also checked
   that public list and saw the license had moved to a different owner, it could mark
   the account Inactive on its own instead of flagging it.
3. **Tell reps what changed.** Compare today's website to yesterday's and post a short
   daily note ("Bellhaven added Findlay; Bellhaven dropped Alliance"). That is a
   heads-up for sales, not just a records cleanup.
4. **Other operators.** Only the scraper knows what Bellhaven's website looks like;
   the matching, the SOP rules, and the review app do not care whose facilities they
   are. Covering Juniper Point or Stonebridge means writing one small scraper for their
   site and reusing everything else. Sixty percent of facilities have a corporate
   parent; this is how one pipeline covers all of them.
