"""Decision ledger: the idempotency store.

Every proposal has a stable id (hash of type + subject + desired changes). Once a
reviewer approves or rejects it, the id is recorded here and the pipeline never
re-proposes it. An approval whose write failed is *not* considered decided, so it
comes back on the next run, but every attempt is kept, so an account created by
a failed attempt is reused rather than created again.
"""
import datetime as dt
import json

from . import config


class Ledger:
    def __init__(self, path=None):
        self.path = path or config.DECISIONS
        self.data: dict = json.loads(self.path.read_text()) if self.path.exists() else {}

    def _save(self):
        self.path.parent.mkdir(exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=1))

    def get(self, pid: str) -> dict | None:
        return self.data.get(pid)

    def is_decided(self, pid: str) -> bool:
        d = self.data.get(pid)
        if not d:
            return False
        if d["decision"] == "rejected":
            return True
        return d.get("result", {}).get("status") == "applied"

    def record(self, proposal: dict, decision: str, result: dict | None = None) -> dict:
        now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        prev = self.data.get(proposal["id"]) or {}
        entry = {
            "decision": decision,
            "decided_at": now,
            "type": proposal["type"],
            "subject_key": proposal["subject_key"],
            "title": proposal.get("title"),
            "location": (proposal.get("location") or {}).get("name"),
            "account": (proposal.get("account") or {}).get("name"),
            "account_id": (proposal.get("account") or {}).get("account_id"),
            "changes": proposal["changes"],
            "result": result or {},
            "attempts": prev.get("attempts", []) + [{"decision": decision, "decided_at": now, "result": result or {}}],
        }
        self.data[proposal["id"]] = entry
        self._save()
        return entry

    def prior_created(self, pid: str) -> str | None:
        """An account id created by an earlier (failed) attempt at this proposal."""
        for att in reversed((self.data.get(pid) or {}).get("attempts", [])):
            if att.get("result", {}).get("created_account_id"):
                return att["result"]["created_account_id"]
        return None

    def match_links(self) -> dict[str, str]:
        """Reviewer identity decisions: {'<slug>|<account_id>': 'approved'|'rejected'}."""
        out = {}
        for d in self.data.values():
            if d["type"] == "CONFIRM_MATCH" and self.is_decided_entry(d):
                out[d["subject_key"]] = "rejected" if d["decision"] == "rejected" else "approved"
        return out

    @staticmethod
    def is_decided_entry(d: dict) -> bool:
        return d["decision"] == "rejected" or d.get("result", {}).get("status") == "applied"

    def history(self) -> list[tuple[str, dict]]:
        return sorted(self.data.items(), key=lambda kv: kv[1]["decided_at"], reverse=True)
