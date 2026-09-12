#!/usr/bin/env python3
"""Local review UI. Shows every proposal with its evidence; approve writes to the
CRM through the API and records the decision; reject records it. Nothing is
written without a click.

    python review_app.py            # http://127.0.0.1:5055
"""
import json

from flask import Flask, abort, flash, redirect, render_template, request, url_for

from pipeline import config
from pipeline.apply import apply_proposal
from pipeline.crm import CRM
from pipeline.ledger import Ledger
import run_pipeline

app = Flask(__name__)
app.secret_key = "local-review-only"

# Reviewer order. CHOW before DUPLICATE because a duplicate of a CHOW'd account
# points at the *new* account id.
ORDER = ["CHOW", "REPARENT", "DUPLICATE", "CREATE", "UPDATE_NAME", "UPDATE_ADDRESS",
         "UPDATE_CARE_TYPE", "UPDATE_PHONE", "REACTIVATE", "NOT_ON_SITE"]
LABEL = {
    "CHOW": "Change of ownership", "REPARENT": "Move under parent", "DUPLICATE": "Duplicate account",
    "CREATE": "New account", "UPDATE_NAME": "Rename", "UPDATE_ADDRESS": "Fix address",
    "UPDATE_CARE_TYPE": "Fix care type", "UPDATE_PHONE": "Fix phone", "REACTIVATE": "Reactivate",
    "NOT_ON_SITE": "Not on website", "CONFIRM": "Already matches",
}
EXPLAIN = {
    "CHOW": "The account belongs under a different parent, but it has revenue and open AR. Billing needs the old account preserved, so a successor account is created under the correct parent and the old one is linked to it. Nothing else on the old account changes.",
    "REPARENT": "The account belongs under a different parent and has no billing exposure (no revenue, or no open AR), so it moves directly.",
    "DUPLICATE": "More than one CRM account describes this building. The losing copy is marked Inactive and linked to the survivor. The API has no merge, so this is the prescribed way to retire it.",
    "CREATE": "The website lists this community and no CRM account resolves to it.",
    "UPDATE_NAME": "Same building, different name in the CRM. Address or phone agree, so this is a rename rather than a different facility.",
    "UPDATE_ADDRESS": "Same building, stale address in the CRM.",
    "UPDATE_CARE_TYPE": "The CRM care type is empty or not among what the website offers.",
    "UPDATE_PHONE": "The CRM phone differs from the website listing.",
    "REACTIVATE": "The account is Inactive but the community is live on the website.",
    "NOT_ON_SITE": "The account is under the parent but the website no longer lists it. Absence alone is weak evidence of a sale or closure, so it is flagged for a person rather than deactivated.",
    "CONFIRM": "Website and CRM agree on every field the pipeline checks. No change is proposed.",
}


def load_queue() -> dict:
    if not config.PROPOSALS.exists():
        return {"proposals": [], "confirmed": [], "counts": {}, "parent": {}}
    return json.loads(config.PROPOSALS.read_text())


def open_items(q: dict, ledger: Ledger) -> list[dict]:
    props = [p for p in q["proposals"] if not ledger.is_decided(p["id"])]
    props.sort(key=lambda p: (ORDER.index(p["type"]) if p["type"] in ORDER else 99, -p["confidence"]))
    return props


def groups_for(props: list[dict], confirmed: list[dict]) -> list[dict]:
    out = []
    for t in ORDER:
        items = [p for p in props if p["type"] == t]
        if items:
            out.append({"type": t, "label": LABEL[t], "entries": items})
    if confirmed:
        out.append({"type": "CONFIRM", "label": LABEL["CONFIRM"],
                    "entries": [{"id": "c:" + c["account_id"], "title": c["location"], "sub": c["account"], "confidence": c["confidence"]} for c in confirmed]})
    return out


def _dash(v):
    return v if v not in (None, "") else "—"


def display(v):
    """Parent accounts are named '... (Parent Account)' in the CRM; drop the suffix for reading."""
    return v.replace(" (Parent Account)", "") if isinstance(v, str) else v


app.jinja_env.filters["display"] = display


def recon_rows(p: dict, parent: dict, names: dict | None = None) -> list[dict]:
    """Field-aligned worksheet: what the website says, what the CRM has now, what it
    will have after approval. `changed` marks cells the approval touches."""
    L, A = p.get("location"), p.get("account")
    ch = {c["field"]: c["to"] for c in p["changes"]}
    t = p["type"]

    def row(field, web, crm, after=None, changed=False, note=""):
        return {"field": field, "web": _dash(web), "crm": _dash(crm), "after": _dash(after if after is not None else crm), "changed": changed, "note": note}

    rows = []
    if t == "CREATE":
        pl = p["actions"][0]["payload"]
        for f, key in (("Name", "name"), ("Parent", "parent_id"), ("Street", "billing_street"), ("City", "billing_city"),
                       ("State", "billing_state"), ("ZIP", "billing_zip"), ("Care type", "care_type"), ("Phone", "phone"), ("Status", "status")):
            val = display(parent["name"]) if key == "parent_id" else pl[key]
            rows.append(row(f, val, None, val, True))
        return rows

    if t == "NOT_ON_SITE":
        rows.append(row("Listed on website", "no", "under " + display(A["parent_name"] or "no parent")))
        rows.append(row("Status", None, A["status"], ch.get("status"), True))
        rows.append(row("Lifetime revenue", None, f"${A['lifetime_revenue']:,}"))
        rows.append(row("Outstanding AR", None, f"${A['outstanding_ar']:,}"))
        return rows

    # matched location vs existing account
    rows.append(row("Name", L["name"], A["name"], ch.get("name"), "name" in ch))
    if t == "CHOW":
        rows.append(row("Parent", display(parent["name"]), display(A["parent_name"] or "none"), display(A["parent_name"] or "none"), False,
                        "stays as is; successor account is created under " + display(parent["name"])))
        rows.append(row("Change of ownership link", None, A["chow_current_account"], "new successor account", True))
    elif "parent_id" in ch:
        rows.append(row("Parent", display(parent["name"]), display(A["parent_name"] or "none"), display(parent["name"]), True))
    else:
        rows.append(row("Parent", display(parent["name"]), display(A["parent_name"] or "none")))
    rows.append(row("Street", L["street"], A["billing_street"], ch.get("billing_street"), "billing_street" in ch))
    rows.append(row("City, state ZIP", f"{L['city']}, {L['state']} {L['zip']}", f"{A['billing_city']}, {A['billing_state']} {A['billing_zip']}",
                    f"{ch.get('billing_city', A['billing_city'])}, {ch.get('billing_state', A['billing_state'])} {ch.get('billing_zip', A['billing_zip'])}",
                    any(k in ch for k in ("billing_city", "billing_state", "billing_zip"))))
    rows.append(row("Care", ", ".join(L["care_offerings"]), A["care_type"], ch.get("care_type"), "care_type" in ch))
    rows.append(row("Phone", L["phone"], A["phone"], ch.get("phone"), "phone" in ch))
    rows.append(row("Status", "on website", A["status"], ch.get("status"), "status" in ch))
    if t == "DUPLICATE":
        surv = ch.get("duplicate_of_account")
        if isinstance(surv, str):
            surv_txt = f"{(names or {}).get(surv, '')} {surv}".strip()
        else:
            surv_txt = "successor account of " + surv.get("$chow_new_of", "")
        rows.append(row("Duplicate of", None, A["duplicate_of_account"], surv_txt, True))
    rows.append(row("Lifetime revenue", None, f"${A['lifetime_revenue']:,}"))
    rows.append(row("Outstanding AR", None, f"${A['outstanding_ar']:,}"))
    return rows


def _render(selected_id: str | None):
    q = load_queue()
    ledger = Ledger()
    props = open_items(q, ledger)
    groups = groups_for(props, q.get("confirmed", []))
    parent = q.get("parent") or {}
    sel, kind, rows, successor = None, None, [], None
    if selected_id and selected_id.startswith("c:"):
        c = next((c for c in q.get("confirmed", []) if "c:" + c["account_id"] == selected_id), None)
        if not c:
            abort(404)
        sel, kind = {"id": selected_id, "type": "CONFIRM", "location": {"name": c["location"]}, "account": {"name": c["account"], "account_id": c["account_id"]},
                     "confidence": c["confidence"], "confidence_band": "high", "evidence": c["evidence"], "changes": [], "rationale": EXPLAIN["CONFIRM"], "attention": ""}, "confirm"
    elif selected_id:
        sel = next((p for p in props if p["id"] == selected_id), None)
        if not sel:
            abort(404)
        names = {}
        if config.CRM_SNAPSHOT.exists():
            names = {a["account_id"]: a["name"] for a in json.loads(config.CRM_SNAPSHOT.read_text())}
        kind, rows = "proposal", recon_rows(sel, parent, names)
        if sel["type"] == "CHOW":
            successor = sel["actions"][0]["payload"]
    return render_template("review.html", q=q, groups=groups, total=len(props), sel=sel, kind=kind, rows=rows,
                           successor=successor, label=LABEL, explain=EXPLAIN, parent=parent, history_count=len(ledger.data))


@app.get("/")
def index():
    q = load_queue()
    props = open_items(q, Ledger())
    if props:
        return redirect(url_for("show", pid=props[0]["id"]))
    return _render(None)


@app.get("/p/<pid>")
def show(pid):
    return _render(pid)


@app.post("/decide/<pid>")
def decide(pid):
    decision = request.form["decision"]
    q = load_queue()
    ledger = Ledger()
    props = open_items(q, ledger)
    ids = [p["id"] for p in props]
    p = next((x for x in props if x["id"] == pid), None)
    if not p:
        flash(("error", "That item is no longer in the queue."))
        return redirect(url_for("index"))
    label = f"{LABEL[p['type']]}: {(p.get('location') or {}).get('name') or (p.get('account') or {}).get('name')}"
    if decision == "reject":
        ledger.record(p, "rejected")
        flash(("ok", f"Rejected. {label}."))
    else:
        result = apply_proposal(p, CRM(), ledger)
        ledger.record(p, "approved", result)
        if result["status"] == "applied":
            flash(("ok", f"Approved. {label}. " + "; ".join(result["log"]) + "."))
        else:
            flash(("error", f"Not written ({result['status']}). {label}. {result['detail']}"))
    i = ids.index(pid)
    nxt = ids[i + 1] if i + 1 < len(ids) else (ids[i - 1] if i > 0 else None)
    return redirect(url_for("show", pid=nxt) if nxt else url_for("index"))


@app.post("/run")
def run():
    out = run_pipeline.run(no_scrape=bool(request.form.get("no_scrape")))
    flash(("ok", f"Pipeline finished. {out['counts']['site_locations']} website locations checked against {out['counts']['crm_accounts']} CRM accounts: "
                 f"{len(out['proposals'])} to review, {out['counts']['confirmed']} already match, {out['suppressed']} skipped as previously decided."))
    return redirect(url_for("index"))


@app.get("/history")
def history():
    return render_template("history.html", history=Ledger().history(), label=LABEL, q=load_queue())


if __name__ == "__main__":
    app.run(port=5055, debug=False)
