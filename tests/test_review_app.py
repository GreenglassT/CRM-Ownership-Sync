"""Review app decide endpoint: strict decision values, no double-apply."""
import json
import threading
import time

import pytest

import review_app
from pipeline import config


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROPOSALS", tmp_path / "proposals.json")
    monkeypatch.setattr(config, "DECISIONS", tmp_path / "decisions.json")
    p = {"id": "p1", "type": "UPDATE_NAME", "title": "X", "subject_key": "A", "confidence": 1.0, "confidence_band": "high",
         "location": {"name": "X"}, "account": {"account_id": "A", "name": "Old"}, "changes": [], "actions": [{"op": "patch", "account_id": "A", "payload": {"name": "X"}}],
         "evidence": [], "rationale": "", "attention": "", "near_misses": []}
    config.PROPOSALS.write_text(json.dumps({"proposals": [p], "confirmed": [], "counts": {}, "parent": {"name": "P"}}))
    monkeypatch.setattr(review_app, "CRM", lambda: object())
    calls = []
    def slow_apply(p, crm, ledger):
        calls.append(p["id"]); time.sleep(0.2)
        return {"status": "applied", "detail": "ok", "log": [], "created_account_id": None}
    monkeypatch.setattr(review_app, "apply_proposal", slow_apply)
    review_app.app.config["TESTING"] = True
    c = review_app.app.test_client()
    c.calls = calls
    return c


def test_unknown_decision_value_is_rejected(app):
    r = app.post("/decide/p1", data={"decision": "cancel"})
    assert r.status_code == 400 and app.calls == []


def test_concurrent_approvals_apply_once(app):
    def go():
        app.post("/decide/p1", data={"decision": "approve"})
    ts = [threading.Thread(target=go) for _ in range(2)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert app.calls == ["p1"]


def test_decided_item_page_redirects_instead_of_404(app):
    app.post("/decide/p1", data={"decision": "reject"})
    r = app.get("/p/p1")
    assert r.status_code == 302
