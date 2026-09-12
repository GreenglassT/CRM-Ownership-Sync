"""Decision ledger: the idempotency store.

Every proposal has a stable id (hash of type + subject + desired changes). Once a
reviewer approves or rejects it, the id is recorded here and the pipeline never
re-proposes it. An approval whose write failed is *not* considered decided, so it
comes back on the next run.
"""
import datetime as dt
import json

from . import config


class Ledger:
    def __init__(self, path=config.DECISIONS):
        self.path = path
        self.data: dict = json.loads(path.read_text()) if path.exists() else {}

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
        entry = {
            "decision": decision,
            "decided_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "type": proposal["type"],
            "subject_key": proposal["subject_key"],
            "location": (proposal.get("location") or {}).get("name"),
            "account": (proposal.get("account") or {}).get("name"),
            "account_id": (proposal.get("account") or {}).get("account_id"),
            "changes": proposal["changes"],
            "result": result or {},
        }
        self.data[proposal["id"]] = entry
        self._save()
        return entry

    def created_account_for(self, old_account_id: str) -> str | None:
        """The new account id produced by an applied CHOW on old_account_id."""
        for d in self.data.values():
            if d["type"] == "CHOW" and d["subject_key"] == old_account_id and d.get("result", {}).get("status") == "applied":
                return d["result"].get("created_account_id")
        return None

    def history(self) -> list[tuple[str, dict]]:
        return sorted(self.data.items(), key=lambda kv: kv[1]["decided_at"], reverse=True)
