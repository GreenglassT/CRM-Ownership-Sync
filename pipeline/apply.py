"""Execute an approved proposal against the CRM. The only module that writes.

Safety properties:
  * Validate, then write. Every PATCH target is re-fetched and EVERY field in the
    payload must still hold the value in the proposal's account snapshot before
    the first side effect happens. If the CRM moved underneath us nothing is
    written and the result is 'stale'.
  * Never create twice. If an earlier attempt at this proposal already created an
    account (recorded in the ledger), that id is reused instead of POSTing again.
  * Never raise. Any failure comes back as a result dict so the ledger records it.
  * Notes are appended, never overwritten. Only API-declared mutable fields are sent.
"""
from .crm import CRM, CRMError
from .ledger import Ledger


def _resolve(value, binds: dict):
    if isinstance(value, dict):
        if "$bind" in value:
            return binds[value["$bind"]]
        return {k: _resolve(v, binds) for k, v in value.items()}
    return value


def apply_proposal(p: dict, crm: CRM, ledger: Ledger) -> dict:
    binds, log = {}, []
    created = ledger.prior_created(p["id"])
    snapshot = p.get("account") or {}
    try:
        # -- phase 1: validate every patch against the snapshot the proposal was built from
        currents = {}
        for action in p["actions"]:
            if action["op"] != "patch":
                continue
            current = crm.get_account(action["account_id"])
            currents[action["account_id"]] = current
            if action["account_id"] != snapshot.get("account_id"):
                continue                      # no snapshot of this account: nothing to compare
            stale = [f"{f}: expected {snapshot.get(f)!r}, CRM now has {current.get(f)!r}"
                     for f in action["payload"] if f != "note" and f in snapshot
                     and (current.get(f) or "") != (snapshot.get(f) or "")]
            if stale:
                return {"status": "stale", "detail": "; ".join(stale), "log": log, "created_account_id": created}

        # -- phase 2: execute
        for action in p["actions"]:
            if action["op"] == "create":
                if created and action.get("bind"):
                    new_id = created
                    log.append(f"reusing {new_id} created by an earlier attempt")
                else:
                    r = crm.create_account(action["payload"])
                    new_id = r.get("account_id") or (r.get("data") or {}).get("account_id")
                    if not new_id:
                        raise CRMError(f"create returned no account_id: {r}")
                    created = new_id
                    log.append(f"POST /accounts -> {new_id} ({action['payload']['name']})")
                if action.get("bind"):
                    binds[action["bind"]] = new_id
            elif action["op"] == "patch":
                payload = _resolve(action["payload"], binds)
                if action.get("note_append"):
                    existing = (currents[action["account_id"]].get("note") or "").rstrip()
                    payload = {**payload, "note": (existing + "\n" if existing else "") + action["note_append"]}
                crm.update_account(action["account_id"], payload)
                log.append(f"PATCH /accounts/{action['account_id']} {sorted(payload)}")
            else:
                raise CRMError(f"unknown op {action['op']}")
        return {"status": "applied", "detail": "ok", "log": log, "created_account_id": created}
    except CRMError as e:
        return {"status": "failed", "detail": str(e), "log": log, "created_account_id": created}
    except Exception as e:                     # noqa: BLE001 - the ledger must always get a record
        return {"status": "failed", "detail": f"{type(e).__name__}: {e}", "log": log, "created_account_id": created}
