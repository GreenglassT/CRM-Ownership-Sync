"""Execute an approved proposal against the CRM. The only module that writes.

Safety properties:
  * Optimistic concurrency: before each PATCH the account is re-fetched and every
    field we are about to change must still hold the value the proposal was built
    from. If the CRM moved underneath us the write is skipped and reported 'stale'
    so the next pipeline run can re-evaluate.
  * Notes are appended, never overwritten.
  * Only fields the API declares mutable are ever sent (enforced in crm.py).
"""
from .crm import CRM, CRMError
from .ledger import Ledger


class Blocked(Exception):
    pass


def _resolve(value, binds: dict, ledger: Ledger):
    if isinstance(value, dict):
        if "$bind" in value:
            return binds[value["$bind"]]
        if "$chow_new_of" in value:
            new_id = ledger.created_account_for(value["$chow_new_of"])
            if not new_id:
                raise Blocked(f"approve the CHOW for {value['$chow_new_of']} first; this duplicate must point at the new account")
            return new_id
        return {k: _resolve(v, binds, ledger) for k, v in value.items()}
    return value


def apply_proposal(p: dict, crm: CRM, ledger: Ledger) -> dict:
    binds, log, created = {}, [], None
    try:
        for action in p["actions"]:
            if action["op"] == "create":
                r = crm.create_account(action["payload"])
                new_id = r.get("account_id") or (r.get("data") or {}).get("account_id")
                if not new_id:
                    raise CRMError(f"create returned no account_id: {r}")
                created = new_id
                if action.get("bind"):
                    binds[action["bind"]] = new_id
                log.append(f"POST /accounts -> {new_id} ({action['payload']['name']})")
            elif action["op"] == "patch":
                payload = _resolve(action["payload"], binds, ledger)
                current = crm.get_account(action["account_id"])
                stale = []
                for ch in p["changes"]:
                    f = ch["field"]
                    if f in payload and (current.get(f) or "") != (ch["from"] or ""):
                        stale.append(f"{f}: expected {ch['from']!r}, CRM now has {current.get(f)!r}")
                if stale:
                    return {"status": "stale", "detail": "; ".join(stale), "log": log, "created_account_id": created}
                if action.get("note_append"):
                    existing = (current.get("note") or "").rstrip()
                    payload = {**payload, "note": (existing + "\n" if existing else "") + action["note_append"]}
                crm.update_account(action["account_id"], payload)
                log.append(f"PATCH /accounts/{action['account_id']} {sorted(payload)}")
            else:
                raise CRMError(f"unknown op {action['op']}")
        return {"status": "applied", "detail": "ok", "log": log, "created_account_id": created}
    except Blocked as e:
        return {"status": "blocked", "detail": str(e), "log": log, "created_account_id": created}
    except CRMError as e:
        return {"status": "failed", "detail": str(e), "log": log, "created_account_id": created}
