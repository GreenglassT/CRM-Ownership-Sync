"""apply_proposal: validate everything before the first write; never lose a created id."""
import pytest

from pipeline.apply import apply_proposal
from pipeline.crm import CRMError
from pipeline.ledger import Ledger


class FakeCRM:
    def __init__(self, accounts, fail_patch_once=False):
        self.accounts = {a["account_id"]: dict(a) for a in accounts}
        self.posts, self.patches = [], []
        self.fail_patch_once = fail_patch_once
        self.n = 0

    def get_account(self, aid):
        return dict(self.accounts[aid])

    def create_account(self, payload):
        self.n += 1
        aid = f"NEW{self.n}"
        self.posts.append(aid)
        self.accounts[aid] = {"account_id": aid, **payload}
        return {"account_id": aid}

    def update_account(self, aid, payload):
        if self.fail_patch_once:
            self.fail_patch_once = False
            raise CRMError("PATCH -> 503")
        self.patches.append((aid, payload))
        self.accounts[aid].update(payload)
        return {"account_id": aid}


@pytest.fixture
def ledger(tmp_path):
    return Ledger(tmp_path / "decisions.json")


def snapshot(**kw):
    base = {"account_id": "A", "name": "Old", "billing_street": "1 Main St", "billing_city": "Xtown", "billing_state": "OH",
            "billing_zip": "44000", "status": "Active", "parent_id": "P2", "chow_current_account": "", "note": ""}
    base.update(kw)
    return base


def address_proposal(acct):
    return {"id": "p1", "type": "UPDATE_ADDRESS", "subject_key": "A", "account": acct, "location": None, "title": "x",
            "changes": [{"field": "billing_zip", "from": "44000", "to": "44001"}],
            "actions": [{"op": "patch", "account_id": "A", "payload": {"billing_street": "1 Main St", "billing_city": "Xtown",
                                                                        "billing_state": "OH", "billing_zip": "44001"}, "note_append": "n"}]}


def chow_proposal(acct):
    return {"id": "c1", "type": "CHOW", "subject_key": "A", "account": acct, "location": None, "title": "x",
            "changes": [{"field": "chow_current_account", "from": "", "to": "(new account)"}],
            "actions": [{"op": "create", "payload": {"name": "New", "parent_id": "P1"}, "bind": "new_id"},
                        {"op": "patch", "account_id": "A", "payload": {"chow_current_account": {"$bind": "new_id"}}}]}


def test_stale_check_covers_every_payload_field(ledger):
    snap = snapshot()
    crm = FakeCRM([snapshot(billing_city="Xtown Heights")])   # city edited after the snapshot; zip unchanged
    r = apply_proposal(address_proposal(snap), crm, ledger)
    assert r["status"] == "stale" and "billing_city" in r["detail"]
    assert crm.patches == []


def test_chow_validates_before_creating(ledger):
    snap = snapshot()
    crm = FakeCRM([snapshot(chow_current_account="ALREADY")])
    r = apply_proposal(chow_proposal(snap), crm, ledger)
    assert r["status"] == "stale"
    assert crm.posts == []            # nothing was created


def test_chow_retry_reuses_created_account(ledger):
    snap = snapshot()
    crm = FakeCRM([snap], fail_patch_once=True)
    p = chow_proposal(snap)
    r1 = apply_proposal(p, crm, ledger)
    assert r1["status"] == "failed" and r1["created_account_id"] == "NEW1"
    ledger.record(p, "approved", r1)
    assert not ledger.is_decided("c1")
    r2 = apply_proposal(p, crm, ledger)
    assert r2["status"] == "applied" and r2["created_account_id"] == "NEW1"
    assert crm.posts == ["NEW1"]      # no second successor
    assert crm.accounts["A"]["chow_current_account"] == "NEW1"


def test_unexpected_exception_is_reported_not_raised(ledger):
    snap = snapshot()
    crm = FakeCRM([snap])
    crm.get_account = lambda aid: (_ for _ in ()).throw(KeyError("boom"))
    r = apply_proposal(address_proposal(snap), crm, ledger)
    assert r["status"] == "failed" and "boom" in r["detail"]


def test_ledger_keeps_every_attempt(ledger):
    p = chow_proposal(snapshot())
    ledger.record(p, "approved", {"status": "failed", "detail": "x", "log": [], "created_account_id": "NEW1"})
    ledger.record(p, "approved", {"status": "applied", "detail": "ok", "log": [], "created_account_id": "NEW1"})
    e = ledger.get("c1")
    assert len(e["attempts"]) == 2 and e["result"]["status"] == "applied"
    assert ledger.prior_created("c1") == "NEW1"
