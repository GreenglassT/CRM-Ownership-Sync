"""Link every website location to the right CRM account and propose fixes.

Pure function of (site snapshot, CRM snapshot) -> list of proposals. Nothing in
this module talks to the network or writes anything. Every proposal carries the
per-signal evidence that produced it so a reviewer can check the reasoning.

Proposal types
  CREATE            location has no CRM account            -> POST new account under parent
  REPARENT          account sits under the wrong parent    -> PATCH parent_id
  CHOW              wrong parent AND revenue AND open AR   -> POST new account, PATCH old.chow_current_account
  DUPLICATE         two+ accounts resolve to one location  -> PATCH loser: duplicate_of_account, status=Inactive
  UPDATE_NAME       CRM name is stale / rebranded          -> PATCH name
  UPDATE_ADDRESS    CRM address stale / PO box             -> PATCH billing_*
  UPDATE_CARE_TYPE  care_type empty or not offered         -> PATCH care_type
  UPDATE_PHONE      phone empty or differs                 -> PATCH phone
  REACTIVATE        live location but account Inactive     -> PATCH status=Active
  NOT_ON_SITE       under parent, absent from website      -> PATCH status=Needs Review
  CONFIRM_MATCH     best candidate below the confidence bar -> reviewer links or rejects (no write)
  CONFIRM           everything agrees (informational, no action)
"""
import datetime as dt
import hashlib
import json

from . import config
from .normalize import (is_po_box, jaccard, map_care, name_tokens, norm_city, norm_phone,
                        norm_street, norm_zip, primary_care, street_number)

NOTE_TAG = "[ownership-sync]"

# (type, reviewer label, what the type means) in the order a reviewer should work
# the queue: CHOW before DUPLICATE because a duplicate of a CHOW'd account points
# at the *new* account id. The review app derives its labels from this list.
TYPES = [
    ("CONFIRM_MATCH", "Confirm match",
     "The best CRM candidate scores below the confidence bar. Approve to link this website location to the account; the field "
     "fixes are proposed on the next run. Reject if it is a different facility; a new account is proposed instead. Nothing is written either way."),
    ("CHOW", "Change of ownership",
     "The account belongs under a different parent, but it has revenue and open AR. Billing needs the old account preserved, "
     "so a successor account is created under the correct parent and the old one is linked to it. Nothing else on the old account changes."),
    ("REPARENT", "Move under parent",
     "The account belongs under a different parent and has no billing exposure (no revenue, or no open AR), so it moves directly."),
    ("DUPLICATE", "Duplicate account",
     "More than one CRM account describes this building. The losing copy is marked Inactive and linked to the survivor; the API has no merge."),
    ("CREATE", "New account", "The website lists this community and no CRM account resolves to its address or phone."),
    ("UPDATE_NAME", "Rename", "Same building, different name in the CRM. Address or phone agree, so this is a rename, not a different facility."),
    ("UPDATE_ADDRESS", "Fix address", "Same building, stale address in the CRM (PO box, typo, old formatting)."),
    ("UPDATE_CARE_TYPE", "Fix care type", "The CRM care type is empty or not among the website's offerings."),
    ("UPDATE_PHONE", "Fix phone", "The CRM phone is empty or differs from the website listing."),
    ("REACTIVATE", "Reactivate", "The account is Inactive but the community is live on the website."),
    ("NOT_ON_SITE", "Not on website",
     "The account is under the parent but the website no longer lists it. Absence alone is weak evidence of a sale or closure, "
     "so it is flagged Needs Review for a person rather than deactivated."),
    ("CONFIRM", "Already matches", "Website and CRM agree on every field the pipeline checks. No change is proposed."),
]
ORDER = [t for t, _, _ in TYPES]
LABEL = {t: label for t, label, _ in TYPES}
EXPLAIN = {t: explain for t, _, explain in TYPES}


def parent_label(name: str | None) -> str:
    """Parent accounts are named '... (Parent Account)' in the CRM; strip that for reading."""
    return (name or "").replace(" (Parent Account)", "") or "no parent"



# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #
def score_pair(loc: dict, acct: dict) -> tuple[float, list[dict]]:
    """Return (score 0..1, evidence). Positive weights for agreeing identity
    signals, penalties when two *independent* signals contradict each other.
    Evidence rows carry a plain-English label and detail for the reviewer; the
    weight is kept for anyone who wants the arithmetic."""
    ev = []
    s = 0.0

    def add(w, label, detail):
        nonlocal s
        s += w
        ev.append(dict(signal=label, weight=w, detail=detail))

    # --- street -------------------------------------------------------------
    ls, cs = norm_street(loc["street"]), norm_street(acct["billing_street"])
    street_exact = False
    if is_po_box(acct["billing_street"]):
        add(0.0, "CRM has a PO Box", f"{acct['billing_street']}; street not compared")
    elif not cs:
        add(0.0, "CRM street is empty", "street not compared")
    elif ls == cs:
        street_exact = True
        add(0.45, "Same street", loc["street"])
    elif street_number(ls) == street_number(cs) and jaccard(set(ls.split()), set(cs.split())) >= 0.5:
        add(0.25, "Similar street", f"website {loc['street']}, CRM {acct['billing_street']}")
    else:
        add(-0.15, "Different street", f"website {loc['street']}, CRM {acct['billing_street']}")

    # --- zip ----------------------------------------------------------------
    lz, cz = norm_zip(loc["zip"]), norm_zip(acct["billing_zip"])
    if lz and cz:
        if lz == cz:
            add(0.20, "Same ZIP", lz)
        else:
            add(-0.05, "Different ZIP", f"website {lz}, CRM {cz}")

    # --- city/state ---------------------------------------------------------
    if not (loc["city"] and acct["billing_city"]):
        add(0.0, "City missing on one side", "not compared")
    elif norm_city(loc["city"]) == norm_city(acct["billing_city"]) and loc["state"].upper() == (acct["billing_state"] or "").upper():
        add(0.15, "Same city", f"{loc['city']}, {loc['state']}")
    else:
        add(-0.10, "Different city", f"website {loc['city']}, {loc['state']}; CRM {acct['billing_city']}, {acct['billing_state']}")

    # --- phone --------------------------------------------------------------
    lp, cp = norm_phone(loc["phone"]), norm_phone(acct["phone"])
    if lp and cp:
        if lp == cp:
            add(0.35, "Same phone", loc["phone"])
        else:
            # A stale phone on a record at the exact same address is common; a
            # different phone at a different street is two independent contradictions.
            add(-0.10 if street_exact else -0.30, "Different phone", f"website {loc['phone']}, CRM {acct['phone']}")

    # --- name ---------------------------------------------------------------
    j = jaccard(name_tokens(loc["name"]), name_tokens(acct["name"]))
    label = "Same name" if j == 1 else "Name mostly matches" if j >= 0.5 else "Name partly matches" if j > 0 else "Different name"
    add(round(0.25 * j, 3), label, f"website {loc['name']}, CRM {acct['name']}" if j < 1 else loc["name"])

    return max(0.0, min(1.0, round(s, 3))), ev


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def find_parent(crm: list[dict]) -> dict:
    if config.PARENT_ID_OVERRIDE:
        a = next((a for a in crm if a["account_id"] == config.PARENT_ID_OVERRIDE), None)
        if not a:
            raise RuntimeError(f"BELLHAVEN_PARENT_ID {config.PARENT_ID_OVERRIDE!r} is not in the CRM snapshot")
        if a["parent_id"]:
            raise RuntimeError(f"BELLHAVEN_PARENT_ID {config.PARENT_ID_OVERRIDE!r} is not a parent account: {a['name']!r} has parent_id {a['parent_id']!r}")
        return a
    cands = [a for a in crm if not a["parent_id"] and a["name"].lower().startswith(config.OPERATOR_NAME.lower())]
    if len(cands) != 1:
        raise RuntimeError(f"expected exactly one parent account for {config.OPERATOR_NAME!r}, found {len(cands)}")
    return cands[0]


def is_superseded(acct: dict) -> bool:
    """Accounts already handed off (CHOW) or merged (duplicate) are never re-matched."""
    return bool(acct.get("chow_current_account")) or bool(acct.get("duplicate_of_account"))


def has_billing_history(acct: dict) -> bool:
    return (acct.get("lifetime_revenue") or 0) > 0


def has_open_ar(acct: dict) -> bool:
    return (acct.get("outstanding_ar") or 0) > 0


def survivor_rank(acct: dict, parent_id: str, match_score: float = 0.0, successors: frozenset = frozenset()) -> tuple:
    """Which duplicate survives. A CHOW successor always survives (it was created
    on purpose and starts with no billing), then billing history dominates (never
    orphan an AR balance), then the copy already under the right parent, then
    Active, then the copy that agrees best with the website, then completeness.
    Ties break on account_id so the choice is stable run to run."""
    completeness = sum(1 for k in ("billing_street", "billing_zip", "phone", "care_type") if acct.get(k)) / 4
    return (
        8 * (acct["account_id"] in successors)
        + 4 * has_open_ar(acct) + 2 * has_billing_history(acct)
        + 1 * (acct["parent_id"] == parent_id) + 0.5 * (acct["status"] == "Active")
        + 0.4 * match_score + 0.25 * completeness,
        [-ord(c) for c in acct["account_id"]],   # lower id wins a pure tie
    )


def survivor_reason(surv: dict, others: list[dict], parent_id: str, successors: frozenset) -> str:
    """Why this copy is kept, in the reviewer's words."""
    if surv["account_id"] in successors:
        why = "it is the account the change of ownership created"
    elif has_open_ar(surv):
        why = f"it has {money(surv['outstanding_ar'])} in open AR"
    elif has_billing_history(surv):
        why = f"it has {money(surv['lifetime_revenue'])} in billing history"
    elif surv["parent_id"] == parent_id and any(o["parent_id"] != parent_id for o in others):
        why = f"it is the copy already under {config.OPERATOR_NAME}"
    elif surv["status"] == "Active" and any(o["status"] != "Active" for o in others):
        why = "it is the copy that is still Active"
    else:
        why = "it agrees best with the website (the copies are otherwise equivalent)"
    money_note = "" if any(has_billing_history(a) or has_open_ar(a) for a in [surv, *others]) else " No copy has revenue or open AR."
    return f"{surv['name']} is kept because {why}.{money_note}"


def fingerprint(kind: str, subject: str, changes, actions=()) -> str:
    """Stable id for 'this exact change to this subject'. The write payloads are
    part of it, so a proposal whose payload was refreshed by a later run cannot be
    approved under an id the reviewer saw with different values. Dated note lines
    are excluded so ids do not drift day to day."""
    writes = [{k: v for k, v in a.get("payload", {}).items() if k != "note"} for a in actions]
    raw = json.dumps({"t": kind, "s": subject, "c": changes, "w": writes}, sort_keys=True)
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


def today() -> str:
    return dt.date.today().isoformat()


def note_line(text: str) -> str:
    return f"{NOTE_TAG} {today()}: {text}"


def money(v) -> str:
    return f"${v or 0:,}"


def account_payload_from_location(loc: dict, parent_id: str, note: str) -> dict:
    return {
        "name": loc["name"],
        "parent_id": parent_id,
        "status": "Active",
        "billing_street": loc["street"],
        "billing_city": loc["city"],
        "billing_state": loc["state"],
        "billing_zip": loc["zip"],
        "care_type": primary_care(loc["care_offerings"]),
        "phone": loc["phone"],
        "note": note_line(note),
    }


def _proposal(kind, subject_key, loc, acct, changes, actions, evidence, rationale, confidence, attention="", near_misses=None):
    return {
        "id": fingerprint(kind, subject_key, changes, actions),
        "type": kind,
        "title": (loc or acct)["name"],
        "confidence": confidence,
        "confidence_band": "high" if confidence >= config.CONFIDENT else "low",
        "subject_key": subject_key,
        "location": loc,
        "account": acct,
        "changes": changes,
        "actions": actions,
        "evidence": evidence,
        "rationale": rationale,
        "attention": attention,
        "near_misses": near_misses or [],
    }


def _patch(account_id, payload, note=None):
    a = {"op": "patch", "account_id": account_id, "payload": payload}
    if note:
        a["note_append"] = note_line(note)
    return a


def _patch_proposal(kind, acct, loc, new, note, why, evidence, confidence, attention="", changes=None, near_misses=None):
    """A proposal that PATCHes `new` onto one account. `changes` defaults to one
    row per payload field; pass it explicitly when the display diff should be
    narrower than the payload."""
    if changes is None:
        changes = [{"field": k, "from": acct[k], "to": v} for k, v in new.items()]
    return _proposal(kind, acct["account_id"], loc, acct, changes, [_patch(acct["account_id"], new, note)],
                     evidence, why, confidence, attention, near_misses)


# --------------------------------------------------------------------------- #
# Main classification
# --------------------------------------------------------------------------- #
def address_complete(loc: dict) -> bool:
    return all(loc.get(k) for k in ("street", "city", "state", "zip"))


def build_proposals(site: list[dict], crm: list[dict], links: dict | None = None) -> dict:
    """`links` holds the reviewer's identity decisions for low-confidence pairs:
    {'<slug>|<account_id>': 'approved' | 'rejected'} (see Ledger.match_links)."""
    links = links or {}
    parent = find_parent(crm)
    pid = parent["account_id"]
    pname = parent_label(parent["name"])
    # Accounts created by an approved CHOW: they must never lose a duplicate contest.
    successors = frozenset(a["chow_current_account"] for a in crm if a.get("chow_current_account"))
    # Facility accounts = everything that is not a parent/corporate account.
    facilities = [a for a in crm if a["account_id"] != pid and not a["name"].endswith("(Parent Account)")]
    candidates = [a for a in facilities if not is_superseded(a)]

    # 1) score every (location, account) pair
    scored = {}
    for loc in site:
        rows = []
        for a in candidates:
            sc, ev = score_pair(loc, a)
            rows.append((sc, a, ev))
        rows.sort(key=lambda r: -r[0])
        scored[loc["slug"]] = rows

    # 2) assign accounts to locations, best-first, each account claimed at most once
    order = sorted(site, key=lambda l: -(scored[l["slug"]][0][0] if scored[l["slug"]] else 0))
    claimed: set[str] = set()
    proposals, confirms = [], []

    def positive(ev):
        return sum(e["weight"] for e in ev if e["weight"] > 0)

    for loc in order:
        def link(a):
            return links.get(f"{loc['slug']}|{a['account_id']}")

        rows = [(sc, a, ev) for sc, a, ev in scored[loc["slug"]]
                if a["account_id"] not in claimed and link(a) != "rejected"]
        addr_ok = address_complete(loc)
        addr_note = "" if addr_ok else "Website address did not parse (city/state/zip missing); the address was not compared or proposed."
        # "Considered and rejected": rank by *positive* agreement so a candidate that
        # agreed on zip/city/name but was sunk by street+phone conflicts still shows
        # up with the reasons it lost. Net score alone would hide exactly those.
        near = [dict(account_id=a["account_id"], name=a["name"], parent=a["parent_name"], score=sc,
                     street=a["billing_street"], city=a["billing_city"], phone=a["phone"], evidence=ev)
                for sc, a, ev in sorted(rows, key=lambda r: -positive(r[2]))[:3] if positive(ev) >= 0.35]
        near.sort(key=lambda n: -n["score"])
        possible = [(sc, a, ev) for sc, a, ev in rows if sc >= config.POSSIBLE]
        confident = [(sc, a, ev) for sc, a, ev in possible if sc >= config.CONFIDENT or link(a) == "approved"]

        # ---- CREATE -----------------------------------------------------------
        if not possible:
            rationale = f"No CRM account is at this address or has this phone, so {loc['name']} gets a new account under {pname}."
            if near:
                rationale += (f" The closest existing account, {near[0]['name']}, was ruled out: "
                              + "; ".join(e["signal"].lower() + " (" + e["detail"] + ")" for e in near[0]["evidence"] if e["weight"] < 0) + ".")
            payload = account_payload_from_location(loc, pid, f"Created from website listing {loc['url']}. Offerings: {', '.join(loc['care_offerings'])}. Administrator: {loc['administrator']}")
            proposals.append(_proposal("CREATE", loc["slug"], loc, None,
                                       [{"field": "(new account)", "from": None, "to": loc["name"]}],
                                       [{"op": "create", "payload": payload}], [], rationale, 1.0,
                                       "" if addr_ok else addr_note + " The new account would have blank city/state/zip.", near_misses=near))
            continue

        # ---- low confidence: ask the reviewer to confirm the identity first --------
        if not confident:
            best_sc, best, best_ev = possible[0]
            key = f"{loc['slug']}|{best['account_id']}"
            claimed.add(best["account_id"])
            # Show the reviewer exactly what each answer sets in motion: dry-run the
            # pair as if approved, and name what happens to the location if rejected.
            preview = build_proposals([loc], [parent, best], links={key: "approved"})["proposals"]
            runner_up = next((a for sc, a, ev in possible[1:]), None)
            p = _proposal(
                "CONFIRM_MATCH", key, loc, best,
                [{"field": "linked account", "from": None, "to": best["account_id"]}], [], best_ev,
                f"The closest CRM account is {best['name']}, but the match is only {best_sc:.0%} certain, so nobody decides for you. "
                f"If this is the same building, approve and the fixes are proposed on the next run. If it is a different "
                f"facility, reject and a new account is proposed instead.",
                best_sc, addr_note, near_misses=[n for n in near if n["account_id"] != best["account_id"]])
            def readable(c):            # parent ids mean nothing to a reviewer; show the names
                if c["field"] == "parent_id":
                    return {**c, "from": parent_label(best["parent_name"]), "to": pname}
                return c
            p["if_approved"] = [{"type": f["type"], "label": LABEL[f["type"]], "changes": [readable(c) for c in f["changes"]]} for f in preview]
            p["if_rejected"] = (f"{best['name']} is excluded for this location; the next run asks about the runner-up, "
                                f"{runner_up['name']} ({runner_up['account_id']}), instead." if runner_up else
                                f"{best['name']} is excluded for this location; the next run proposes a new account "
                                f"{loc['name']!r} under {pname}.")
            proposals.append(p)
            continue

        # Duplicate group: every confident candidate, plus weaker candidates that sit
        # at the *exact same street* as the location (a third stale copy of the same
        # building usually fails on name + phone but the address anchor is decisive).
        anchor = norm_street(loc["street"])
        group = confident + [r for r in possible if r not in confident and norm_street(r[1]["billing_street"]) == anchor]
        others = [n for n in near if n["account_id"] not in {a["account_id"] for _, a, _ in group}]

        # ---- pick survivor, mark the rest DUPLICATE -----------------------------
        group.sort(key=lambda r: survivor_rank(r[1], pid, r[0], successors), reverse=True)
        surv_sc, surv, surv_ev = group[0]
        claimed.update(a["account_id"] for _, a, _ in group)
        attention = addr_note
        if sum(1 for _, a, _ in group if has_billing_history(a) or has_open_ar(a)) > 1:
            attention += " More than one duplicate carries billing history; confirm survivor with billing before approving."
        surv_needs_chow = surv["parent_id"] != pid and has_billing_history(surv) and has_open_ar(surv)
        surv_parent = parent_label(surv["parent_name"])
        if surv_needs_chow and group[1:]:
            # The losers must point at the successor, which does not exist yet. Emit
            # only the CHOW now; once it lands the old account is superseded, the
            # successor matches, and the next run proposes these as plain duplicates.
            attention += (" Other copies of this building: " + ", ".join(f"{a['name']} ({a['account_id']})" for _, a, _ in group[1:])
                          + ". They will be proposed as duplicates of the successor after this change of ownership lands.")

        for sc, a, ev in ([] if surv_needs_chow else group[1:]):
            p = _patch_proposal(
                "DUPLICATE", a, loc, {"duplicate_of_account": surv["account_id"], "status": "Inactive"},
                f"Duplicate of {surv['account_id']} ({surv['name']}), the same facility at {loc['street']}, {loc['city']}. "
                f"Survivor chosen by: billing history > correct parent > Active > completeness.",
                (f"Two CRM records named {surv['name']} describe the same building at {loc['street']}, {loc['city']}. "
                 if a["name"].strip().casefold() == surv["name"].strip().casefold() else
                 f"{a['name']} and {surv['name']} are both CRM records for the same building at {loc['street']}, {loc['city']}. ")
                + survivor_reason(surv, [b for _, b, _ in group if b is not surv], pid, successors),
                ev, sc, attention, near_misses=others)
            p["survivor"] = surv
            proposals.append(p)

        # ---- survivor: parent, then field-level fixes -----------------------------
        fixes = []   # proposals on the survivor; empty means the match is confirmed as-is
        attention = attention.strip()
        if surv_needs_chow:
            payload = account_payload_from_location(
                loc, pid,
                f"CHOW from {surv['account_id']} ({surv['name']}, was under {surv_parent}). "
                f"Old account preserved for billing: lifetime_revenue={surv['lifetime_revenue']}, outstanding_ar={surv['outstanding_ar']}. "
                f"Offerings: {', '.join(loc['care_offerings'])}.")
            fixes.append(_proposal(
                "CHOW", surv["account_id"], loc, surv,
                [{"field": "chow_current_account", "from": surv["chow_current_account"], "to": "(new account)"}],
                [{"op": "create", "payload": payload, "bind": "new_id"},
                 {"op": "patch", "account_id": surv["account_id"], "payload": {"chow_current_account": {"$bind": "new_id"}}}],
                surv_ev,
                f"The website lists this as a {config.OPERATOR_NAME} community, but the CRM has it under {surv_parent}. "
                f"This account has {money(surv['lifetime_revenue'])} in lifetime revenue and {money(surv['outstanding_ar'])} still outstanding, "
                f"so billing needs it left exactly as it is. A new account is created under {pname} and the old one is linked to it.",
                surv_sc, attention, near_misses=others))
        else:
            def fix(kind, new, note, why, changes=None):
                fixes.append(_patch_proposal(kind, surv, loc, new, note, why, surv_ev, surv_sc, attention, changes, others))

            if surv["parent_id"] != pid:
                why = "no revenue history" if not has_billing_history(surv) else "no outstanding AR"
                money_why = ("it has no billing history" if not has_billing_history(surv)
                             else f"it has {money(surv['lifetime_revenue'])} in revenue but nothing outstanding")
                fix("REPARENT", {"parent_id": pid},
                    f"Re-parented from {surv_parent} to {pname}; listed at {loc['url']}. SOP: {why}, so direct re-parent is allowed.",
                    f"The website lists this as a {config.OPERATOR_NAME} community, but the CRM has it under {surv_parent}. "
                    f"It can simply be moved because {money_why}, so billing does not need the old record preserved.")
            if surv["name"].strip().lower() != loc["name"].strip().lower():
                fix("UPDATE_NAME", {"name": loc["name"]}, f"Renamed from {surv['name']!r} to match website listing.",
                    f"The website calls this community {loc['name']}; the CRM still says {surv['name']}. Same building, so it is a rename, not a different facility.")
            addr_stale = addr_ok and (
                is_po_box(surv["billing_street"]) or norm_street(surv["billing_street"]) != norm_street(loc["street"])
                or norm_zip(surv["billing_zip"]) != norm_zip(loc["zip"]) or norm_city(surv["billing_city"]) != norm_city(loc["city"])
                or (surv["billing_state"] or "").upper() != loc["state"].upper())
            if addr_stale:
                new = {"billing_street": loc["street"], "billing_city": loc["city"], "billing_state": loc["state"], "billing_zip": loc["zip"]}
                why = "CRM has a PO Box; website has the street address" if is_po_box(surv["billing_street"]) else "address differs from website"
                fix("UPDATE_ADDRESS", new, f"Address updated from website: {why}.",
                    (f"The CRM has a PO Box for this community; the website has the street address, {loc['street']}." if is_po_box(surv["billing_street"])
                     else f"The address in the CRM does not match the website. The record matched on other signals, so this is a correction, not a different building."),
                    changes=[{"field": k, "from": surv[k], "to": v} for k, v in new.items() if (surv[k] or "") != v])
            offered = map_care(loc["care_offerings"])
            if offered and surv["care_type"] not in offered:
                fix("UPDATE_CARE_TYPE", {"care_type": offered[0]}, f"care_type set from website offerings: {', '.join(loc['care_offerings'])}.",
                    f"The website offers {', '.join(loc['care_offerings'])}; the CRM care type ({surv['care_type'] or 'blank'}) is not one of them. Set it to {offered[0]}.")
            if config.PROPOSE_PHONE_UPDATES and loc["phone"] and norm_phone(surv["phone"]) != norm_phone(loc["phone"]):
                fix("UPDATE_PHONE", {"phone": loc["phone"]}, "Phone updated from website.",
                    f"The website lists {loc['phone']} for this community; the CRM has {surv['phone'] or 'no phone'}.")
            if surv["status"] != "Active":
                fix("REACTIVATE", {"status": "Active"}, "Reactivated: facility is listed on the website.",
                    f"The CRM has this account as {surv['status']}, but the community is live on the website.")

        if fixes:
            proposals.extend(fixes)
        else:
            checked = "name, parent, address, care type, status" + (", phone" if config.PROPOSE_PHONE_UPDATES else "")
            confirms.append(_proposal("CONFIRM", surv["account_id"], loc, surv, [], [], surv_ev,
                                      f"The website and the CRM agree on {checked}. Nothing to change.", surv_sc, attention, near_misses=others))

    # ---- NOT_ON_SITE: under our parent, live, not linked to any website location ---
    for a in facilities:
        if a["parent_id"] != pid or a["account_id"] in claimed or is_superseded(a) or a["status"] != "Active":
            continue
        caution = ""
        if has_billing_history(a) or has_open_ar(a):
            caution = f" It has {money(a['lifetime_revenue'])} in revenue and {money(a['outstanding_ar'])} outstanding, so do not deactivate it without billing sign-off."
        proposals.append(_patch_proposal(
            "NOT_ON_SITE", a, None, {"status": "Needs Review"},
            f"Under {pname} but not listed on {config.SITE_BASE}/communities as of {today()}. Possible divestiture or closure; confirm with rep.{caution}",
            f"The CRM has this account under {config.OPERATOR_NAME}, but nothing on the website matches it. A community missing from a website "
            f"is not proof it was sold or closed, so it is flagged for a person to check rather than deactivated.{caution}",
            [{"signal": "Not on the website", "weight": 0, "detail": f"no community near {a['billing_city']}, {a['billing_state']} resembles this account"}],
            1.0, "Has billing history." if caution else ""))

    return {"parent": {"account_id": pid, "name": parent["name"]}, "proposals": proposals, "confirmed": confirms}


def main():
    site = json.loads(config.SITE_SNAPSHOT.read_text())
    crm = json.loads(config.CRM_SNAPSHOT.read_text())
    out = build_proposals(site, crm)
    config.PROPOSALS.write_text(json.dumps(out, indent=1))
    from collections import Counter
    print(Counter(p["type"] for p in out["proposals"]), "confirmed:", len(out["confirmed"]))


if __name__ == "__main__":
    main()
