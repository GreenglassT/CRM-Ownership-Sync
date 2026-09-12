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
  CONFIRM           everything agrees (informational, no action)
"""
import datetime as dt
import hashlib
import json

from . import config
from .normalize import (is_po_box, jaccard, map_care, name_tokens, norm_city, norm_phone,
                        norm_street, norm_zip, primary_care, street_number)

NOTE_TAG = "[ownership-sync]"


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #
def score_pair(loc: dict, acct: dict) -> tuple[float, list[dict]]:
    """Return (score 0..1, evidence). Positive weights for agreeing identity
    signals, penalties when two *independent* signals contradict each other."""
    ev = []
    s = 0.0

    # --- street -------------------------------------------------------------
    ls, cs = norm_street(loc["street"]), norm_street(acct["billing_street"])
    street_exact = False
    if is_po_box(acct["billing_street"]):
        ev.append(dict(signal="street", weight=0.0, detail=f"CRM street is a PO Box ({acct['billing_street']!r}); neutral"))
    elif not cs:
        ev.append(dict(signal="street", weight=0.0, detail="CRM street empty; neutral"))
    elif ls == cs:
        street_exact = True
        s += 0.45
        ev.append(dict(signal="street", weight=0.45, detail=f"normalized street equal: {ls!r}"))
    elif street_number(ls) == street_number(cs) and jaccard(set(ls.split()), set(cs.split())) >= 0.5:
        s += 0.25
        ev.append(dict(signal="street", weight=0.25, detail=f"same number, similar street: {ls!r} vs {cs!r}"))
    else:
        s -= 0.15
        ev.append(dict(signal="street", weight=-0.15, detail=f"street conflict: {ls!r} vs {cs!r}"))

    # --- zip ----------------------------------------------------------------
    lz, cz = norm_zip(loc["zip"]), norm_zip(acct["billing_zip"])
    if lz and cz:
        if lz == cz:
            s += 0.20
            ev.append(dict(signal="zip", weight=0.20, detail=f"zip equal: {lz}"))
        else:
            s -= 0.05
            ev.append(dict(signal="zip", weight=-0.05, detail=f"zip differs: {lz} vs {cz}"))

    # --- city/state ---------------------------------------------------------
    if norm_city(loc["city"]) == norm_city(acct["billing_city"]) and loc["state"].upper() == (acct["billing_state"] or "").upper():
        s += 0.15
        ev.append(dict(signal="city", weight=0.15, detail=f"city/state equal: {loc['city']}, {loc['state']}"))
    else:
        s -= 0.10
        ev.append(dict(signal="city", weight=-0.10, detail=f"city/state differs: {loc['city']}, {loc['state']} vs {acct['billing_city']}, {acct['billing_state']}"))

    # --- phone --------------------------------------------------------------
    lp, cp = norm_phone(loc["phone"]), norm_phone(acct["phone"])
    if lp and cp:
        if lp == cp:
            s += 0.35
            ev.append(dict(signal="phone", weight=0.35, detail=f"phone equal: {loc['phone']}"))
        else:
            # A stale phone on a record at the exact same address is common; a
            # different phone at a different street is two independent contradictions.
            pen = 0.10 if street_exact else 0.30
            s -= pen
            ev.append(dict(signal="phone", weight=-pen, detail=f"phone differs: {loc['phone']} vs {acct['phone']}"))

    # --- name ---------------------------------------------------------------
    j = jaccard(name_tokens(loc["name"]), name_tokens(acct["name"]))
    w = round(0.25 * j, 3)
    s += w
    ev.append(dict(signal="name", weight=w, detail=f"name token overlap {j:.2f}: {loc['name']!r} vs {acct['name']!r}"))

    return max(0.0, min(1.0, round(s, 3))), ev


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def find_parent(crm: list[dict]) -> dict:
    if config.PARENT_ID_OVERRIDE:
        return next(a for a in crm if a["account_id"] == config.PARENT_ID_OVERRIDE)
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


def survivor_rank(acct: dict, parent_id: str, match_score: float = 0.0) -> tuple:
    """Which duplicate survives. Billing history dominates (never orphan an AR
    balance), then the copy already under the right parent, then Active, then
    the copy that agrees best with the website, then completeness. Ties break
    on account_id so the choice is stable run to run."""
    completeness = sum(1 for k in ("billing_street", "billing_zip", "phone", "care_type") if acct.get(k)) / 4
    return (
        4 * has_open_ar(acct) + 2 * has_billing_history(acct)
        + 1 * (acct["parent_id"] == parent_id) + 0.5 * (acct["status"] == "Active")
        + 0.4 * match_score + 0.25 * completeness,
        [-ord(c) for c in acct["account_id"]],   # lower id wins a pure tie
    )


def fingerprint(kind: str, subject: str, changes) -> str:
    raw = json.dumps({"t": kind, "s": subject, "c": changes}, sort_keys=True)
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


def today() -> str:
    return dt.date.today().isoformat()


def note_line(text: str) -> str:
    return f"{NOTE_TAG} {today()}: {text}"


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


def _proposal(kind, subject_key, loc, acct, changes, actions, evidence, rationale, confidence, attention=""):
    return {
        "id": fingerprint(kind, subject_key, changes),
        "type": kind,
        "confidence": confidence,
        "confidence_band": "high" if confidence >= config.CONFIDENT else ("low" if confidence > 0 else "n/a"),
        "subject_key": subject_key,
        "location": loc,
        "account": acct,
        "changes": changes,
        "actions": actions,
        "evidence": evidence,
        "rationale": rationale,
        "attention": attention,
    }


def _patch(account_id, payload, note=None):
    a = {"op": "patch", "account_id": account_id, "payload": payload}
    if note:
        a["note_append"] = note_line(note)
    return a


# --------------------------------------------------------------------------- #
# Main classification
# --------------------------------------------------------------------------- #
def build_proposals(site: list[dict], crm: list[dict]) -> dict:
    parent = find_parent(crm)
    pid = parent["account_id"]
    # Facility accounts = everything that is not a parent/corporate account.
    facilities = [a for a in crm if a["account_id"] != pid and not a["name"].endswith("(Parent Account)")]
    candidates = [a for a in facilities if not is_superseded(a)]
    by_id = {a["account_id"]: a for a in crm}

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

    for loc in order:
        rows = [(sc, a, ev) for sc, a, ev in scored[loc["slug"]] if a["account_id"] not in claimed]
        # "Considered and rejected": rank by *positive* agreement so a candidate that
        # agreed on zip/city/name but was sunk by street+phone conflicts still shows
        # up with the reasons it lost. Net score alone would hide exactly those.
        def positive(ev):
            return sum(e["weight"] for e in ev if e["weight"] > 0)
        near = [dict(account_id=a["account_id"], name=a["name"], parent=a["parent_name"], score=sc,
                     street=a["billing_street"], city=a["billing_city"], phone=a["phone"], evidence=ev)
                for sc, a, ev in sorted(rows, key=lambda r: -positive(r[2]))[:3] if positive(ev) >= 0.35]
        near.sort(key=lambda n: -n["score"])
        possible = [(sc, a, ev) for sc, a, ev in rows if sc >= config.POSSIBLE]
        confident = [(sc, a, ev) for sc, a, ev in possible if sc >= config.CONFIDENT]

        # ---- CREATE -----------------------------------------------------------
        if not possible:
            rationale = "No CRM account resolves to this address or phone."
            if near:
                rationale += f" Closest candidate {near[0]['name']!r} ({near[0]['score']:.2f}) was rejected: " + \
                             "; ".join(e["detail"] for e in near[0]["evidence"] if e["weight"] < 0)
            payload = account_payload_from_location(loc, pid, f"Created from website listing {loc['url']}. Offerings: {', '.join(loc['care_offerings'])}. Administrator: {loc['administrator']}")
            p = _proposal("CREATE", loc["slug"], loc, None,
                          [{"field": "(new account)", "from": None, "to": loc["name"]}],
                          [{"op": "create", "payload": payload}],
                          [{"signal": "near_misses", "weight": 0, "detail": json.dumps(near)}] if near else [],
                          rationale, 1.0)
            p["near_misses"] = near
            proposals.append(p)
            continue

        # Duplicate group: every confident candidate, plus weaker candidates that sit
        # at the *exact same street* as the location (a third stale copy of the same
        # building usually fails on name + phone but the address anchor is decisive).
        if confident:
            anchor = norm_street(loc["street"])
            group = confident + [r for r in possible if r not in confident and norm_street(r[1]["billing_street"]) == anchor]
        else:
            group = [possible[0]]
        conf = min(sc for sc, _, _ in group)
        others = [n for n in near if n["account_id"] not in {a["account_id"] for _, a, _ in group}]

        # ---- pick survivor, mark the rest DUPLICATE -----------------------------
        group.sort(key=lambda r: survivor_rank(r[1], pid, r[0]), reverse=True)
        surv_sc, surv, surv_ev = group[0]
        claimed.update(a["account_id"] for _, a, _ in group)
        attention = ""
        if sum(1 for _, a, _ in group if has_billing_history(a) or has_open_ar(a)) > 1:
            attention = "More than one duplicate carries billing history; confirm survivor with billing before approving."

        surv_needs_chow = surv["parent_id"] != pid and has_billing_history(surv) and has_open_ar(surv)
        dup_target = {"$chow_new_of": surv["account_id"]} if surv_needs_chow else surv["account_id"]

        for sc, a, ev in group[1:]:
            changes = [{"field": "duplicate_of_account", "from": a["duplicate_of_account"], "to": dup_target},
                       {"field": "status", "from": a["status"], "to": "Inactive"}]
            note = f"Duplicate of {surv['account_id']} ({surv['name']}) — same facility at {loc['street']}, {loc['city']}. Survivor chosen by: billing history > correct parent > Active > completeness."
            proposals.append(_proposal("DUPLICATE", a["account_id"], loc, a, changes,
                                       [_patch(a["account_id"], {"duplicate_of_account": dup_target, "status": "Inactive"}, note)],
                                       ev, f"{a['name']!r} and {surv['name']!r} both resolve to {loc['name']} ({loc['street']}, {loc['city']}). "
                                           f"Keeping {surv['account_id']} (rank: AR={has_open_ar(surv)}, revenue={has_billing_history(surv)}, "
                                           f"under parent={surv['parent_id']==pid}, {surv['status']}).", sc, attention))

        # ---- survivor: parent -------------------------------------------------
        if surv["parent_id"] != pid:
            if surv_needs_chow:
                payload = account_payload_from_location(
                    loc, pid,
                    f"CHOW from {surv['account_id']} ({surv['name']}, was under {surv['parent_name'] or 'no parent'}). "
                    f"Old account preserved for billing: lifetime_revenue={surv['lifetime_revenue']}, outstanding_ar={surv['outstanding_ar']}. "
                    f"Offerings: {', '.join(loc['care_offerings'])}.")
                changes = [{"field": "chow_current_account", "from": surv["chow_current_account"], "to": "(new account)"}]
                proposals.append(_proposal(
                    "CHOW", surv["account_id"], loc, surv, changes,
                    [{"op": "create", "payload": payload, "bind": "new_id"},
                     {"op": "patch", "account_id": surv["account_id"], "payload": {"chow_current_account": {"$bind": "new_id"}}}],
                    surv_ev,
                    f"Account is under {surv['parent_name'] or 'no parent'} but the website lists it as a {config.OPERATOR_NAME} community. "
                    f"SOP: lifetime_revenue={surv['lifetime_revenue']:,} AND outstanding_ar={surv['outstanding_ar']:,} > 0, so the old account "
                    f"must be left untouched. Create a new account under {config.OPERATOR_NAME} and link old.chow_current_account to it.",
                    surv_sc, attention))
            else:
                why = "no revenue history" if not has_billing_history(surv) else "no outstanding AR"
                changes = [{"field": "parent_id", "from": surv["parent_id"], "to": pid}]
                proposals.append(_proposal(
                    "REPARENT", surv["account_id"], loc, surv, changes,
                    [_patch(surv["account_id"], {"parent_id": pid},
                            f"Re-parented from {surv['parent_name'] or 'no parent'} to {parent['name']}; listed at {loc['url']}. SOP: {why}, so direct re-parent is allowed.")],
                    surv_ev,
                    f"Account is under {surv['parent_name'] or 'no parent'} but the website lists it as a {config.OPERATOR_NAME} community. "
                    f"SOP: {why} (revenue={surv['lifetime_revenue']:,}, AR={surv['outstanding_ar']:,}), so re-parent directly.",
                    surv_sc, attention))

        # ---- survivor: field-level fixes (skipped when CHOW'd: old acct is frozen) ---
        if not surv_needs_chow:
            fixes = 0
            if surv["name"].strip().lower() != loc["name"].strip().lower():
                proposals.append(_proposal("UPDATE_NAME", surv["account_id"], loc, surv,
                                           [{"field": "name", "from": surv["name"], "to": loc["name"]}],
                                           [_patch(surv["account_id"], {"name": loc["name"]}, f"Renamed from {surv['name']!r} to match website listing.")],
                                           surv_ev, f"Website name {loc['name']!r} differs from CRM {surv['name']!r}; same address/phone so this is a rename, not a different facility.",
                                           surv_sc, attention)); fixes += 1
            addr_stale = (is_po_box(surv["billing_street"]) or norm_street(surv["billing_street"]) != norm_street(loc["street"])
                          or norm_zip(surv["billing_zip"]) != norm_zip(loc["zip"]) or norm_city(surv["billing_city"]) != norm_city(loc["city"])
                          or (surv["billing_state"] or "").upper() != loc["state"].upper())
            if addr_stale:
                new = {"billing_street": loc["street"], "billing_city": loc["city"], "billing_state": loc["state"], "billing_zip": loc["zip"]}
                changes = [{"field": k, "from": surv[k], "to": v} for k, v in new.items() if (surv[k] or "") != v]
                why = "CRM has a PO Box; website has the street address" if is_po_box(surv["billing_street"]) else "address differs from website"
                proposals.append(_proposal("UPDATE_ADDRESS", surv["account_id"], loc, surv, changes,
                                           [_patch(surv["account_id"], new, f"Address updated from website: {why}.")],
                                           surv_ev, f"{why.capitalize()}. Matched on other signals, so this is a data-quality fix.", surv_sc, attention)); fixes += 1
            offered = map_care(loc["care_offerings"])
            if offered and surv["care_type"] not in offered:
                proposals.append(_proposal("UPDATE_CARE_TYPE", surv["account_id"], loc, surv,
                                           [{"field": "care_type", "from": surv["care_type"], "to": offered[0]}],
                                           [_patch(surv["account_id"], {"care_type": offered[0]}, f"care_type set from website offerings: {', '.join(loc['care_offerings'])}.")],
                                           surv_ev, f"CRM care_type {surv['care_type']!r} is not among website offerings {offered}; set primary to {offered[0]!r}.", surv_sc, attention)); fixes += 1
            if config.PROPOSE_PHONE_UPDATES and loc["phone"] and norm_phone(surv["phone"]) != norm_phone(loc["phone"]):
                proposals.append(_proposal("UPDATE_PHONE", surv["account_id"], loc, surv,
                                           [{"field": "phone", "from": surv["phone"], "to": loc["phone"]}],
                                           [_patch(surv["account_id"], {"phone": loc["phone"]}, "Phone updated from website.")],
                                           surv_ev, "CRM phone is empty or differs from the website listing.", surv_sc, attention)); fixes += 1
            if surv["status"] != "Active":
                proposals.append(_proposal("REACTIVATE", surv["account_id"], loc, surv,
                                           [{"field": "status", "from": surv["status"], "to": "Active"}],
                                           [_patch(surv["account_id"], {"status": "Active"}, "Reactivated: facility is listed on the website.")],
                                           surv_ev, f"Account is {surv['status']} but the facility is live on the website.", surv_sc, attention)); fixes += 1
            if fixes == 0 and surv["parent_id"] == pid:
                confirms.append({"location": loc["name"], "account_id": surv["account_id"], "account": surv["name"], "confidence": surv_sc, "evidence": surv_ev})
        for p in proposals:
            if p["location"]["slug"] == loc["slug"] and "near_misses" not in p:
                p["near_misses"] = others

    # ---- NOT_ON_SITE: under our parent, live, not linked to any website location ---
    for a in facilities:
        if a["parent_id"] != pid or a["account_id"] in claimed or is_superseded(a) or a["status"] != "Active":
            continue
        caution = ""
        if has_billing_history(a) or has_open_ar(a):
            caution = f" Billing history present (revenue={a['lifetime_revenue']:,}, AR={a['outstanding_ar']:,}) — do not deactivate without billing sign-off."
        changes = [{"field": "status", "from": a["status"], "to": "Needs Review"}]
        proposals.append(_proposal(
            "NOT_ON_SITE", a["account_id"], None, a, changes,
            [_patch(a["account_id"], {"status": "Needs Review"},
                    f"Under {parent['name']} but not listed on {config.SITE_BASE}/communities as of {today()}. Possible divestiture or closure; confirm with rep.{caution}")],
            [{"signal": "website", "weight": 0, "detail": f"No website location within {config.POSSIBLE:.2f} of this account (city {a['billing_city']}, {a['billing_state']})."}],
            f"Account is parented to {config.OPERATOR_NAME} but no website community matches it. Absence from a website is weak evidence of "
            f"divestiture, so flag Needs Review rather than Inactive.{caution}", 1.0,
            "Has billing history." if caution else ""))

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
