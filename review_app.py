#!/usr/bin/env python3
"""Local review UI. Shows every proposal with its evidence; approve writes to the
CRM through the API and records the decision; reject records it. Nothing is
written without a click.

    python review_app.py            # http://127.0.0.1:5055
"""
import json

from flask import Flask, flash, redirect, render_template, request, url_for

from pipeline import config
from pipeline.apply import apply_proposal
from pipeline.crm import CRM
from pipeline.ledger import Ledger
import run_pipeline

app = Flask(__name__)
app.secret_key = "local-review-only"

# Order in which a reviewer should work the queue: CHOW before DUPLICATE because a
# duplicate of a CHOW'd account points at the *new* account id.
ORDER = ["CHOW", "REPARENT", "DUPLICATE", "CREATE", "UPDATE_NAME", "UPDATE_ADDRESS",
         "UPDATE_CARE_TYPE", "UPDATE_PHONE", "REACTIVATE", "NOT_ON_SITE"]
BLURB = {
    "CHOW": "Wrong parent, but the account has revenue AND open AR. SOP: leave it untouched, create a new account under the correct parent, point chow_current_account at it.",
    "REPARENT": "Wrong parent and no billing exposure (no revenue or no open AR). SOP: re-parent the existing account directly.",
    "DUPLICATE": "Two or more accounts resolve to the same website location. The losing copy is marked Inactive with duplicate_of_account set to the survivor.",
    "CREATE": "Website location with no CRM account. Creates it under the operator parent.",
    "UPDATE_NAME": "Same facility (address/phone agree), different name — a rebrand or stale record.",
    "UPDATE_ADDRESS": "Same facility, stale address (PO box, typo, old formatting).",
    "UPDATE_CARE_TYPE": "CRM care_type is empty or not among the website's offerings.",
    "UPDATE_PHONE": "CRM phone differs from the website listing.",
    "REACTIVATE": "Account is Inactive but the facility is live on the website.",
    "NOT_ON_SITE": "Under the operator parent but absent from the website. Flagged Needs Review — absence alone is not proof of divestiture.",
}


def load_queue() -> dict:
    if not config.PROPOSALS.exists():
        return {"proposals": [], "confirmed": [], "counts": {}, "parent": {}}
    return json.loads(config.PROPOSALS.read_text())


def save_queue(q: dict) -> None:
    config.PROPOSALS.write_text(json.dumps(q, indent=1))


@app.get("/")
def index():
    q = load_queue()
    ledger = Ledger()
    props = [p for p in q["proposals"] if not ledger.is_decided(p["id"])]
    props.sort(key=lambda p: (ORDER.index(p["type"]) if p["type"] in ORDER else 99, -p["confidence"]))
    ftype, fband = request.args.get("type", ""), request.args.get("band", "")
    shown = [p for p in props if (not ftype or p["type"] == ftype) and (not fband or p["confidence_band"] == fband)]
    counts = {t: sum(1 for p in props if p["type"] == t) for t in ORDER if any(p["type"] == t for p in props)}
    return render_template("review.html", q=q, props=shown, total=len(props), counts=counts, blurb=BLURB,
                           ftype=ftype, fband=fband, ledger=ledger, history=ledger.history()[:8])


@app.post("/decide/<pid>")
def decide(pid):
    decision = request.form["decision"]
    q = load_queue()
    p = next((x for x in q["proposals"] if x["id"] == pid), None)
    if not p:
        flash(("error", f"proposal {pid} not in queue"))
        return redirect(url_for("index"))
    _decide_one(p, decision)
    return redirect(request.form.get("next") or url_for("index"))


@app.post("/decide-bulk")
def decide_bulk():
    decision = request.form["decision"]
    ids = request.form.getlist("ids")
    q = load_queue()
    by_id = {p["id"]: p for p in q["proposals"]}
    # Apply in reviewer order so CHOW creates land before duplicates that reference them.
    for p in sorted((by_id[i] for i in ids if i in by_id), key=lambda p: ORDER.index(p["type"]) if p["type"] in ORDER else 99):
        _decide_one(p, decision)
    return redirect(url_for("index"))


def _decide_one(p: dict, decision: str) -> None:
    ledger = Ledger()
    label = f"{p['type']} · {(p.get('location') or {}).get('name') or (p.get('account') or {}).get('name')}"
    if decision == "reject":
        ledger.record(p, "rejected")
        flash(("ok", f"Rejected — {label}"))
        return
    result = apply_proposal(p, CRM(), ledger)
    ledger.record(p, "approved", result)
    kind = "ok" if result["status"] == "applied" else "error"
    extra = f" → created {result['created_account_id']}" if result.get("created_account_id") else ""
    flash((kind, f"{result['status'].upper()} — {label}{extra}. {result['detail']}  " + " | ".join(result["log"])))


@app.post("/run")
def run():
    out = run_pipeline.run(no_scrape=bool(request.form.get("no_scrape")))
    flash(("ok", f"Pipeline ran: {out['counts']['site_locations']} site locations, {out['counts']['crm_accounts']} CRM accounts, "
                 f"{len(out['proposals'])} open proposals, {out['suppressed']} suppressed as already decided, {out['counts']['confirmed']} confirmed."))
    return redirect(url_for("index"))


@app.get("/history")
def history():
    return render_template("history.html", history=Ledger().history())


if __name__ == "__main__":
    app.run(port=5055, debug=False)
