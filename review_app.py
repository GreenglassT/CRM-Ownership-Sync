#!/usr/bin/env python3
"""Local review UI. Shows every proposal with its evidence; approve writes to the
CRM through the API and records the decision; reject records it. Nothing is
written without a click.

    python review_app.py            # http://127.0.0.1:5055
"""
import json
import threading

from flask import Flask, abort, flash, redirect, render_template, request, url_for

from pipeline import config
from pipeline.apply import apply_proposal
from pipeline.crm import CRM
from pipeline.ledger import Ledger
from pipeline.match import EXPLAIN, LABEL, ORDER, money, parent_label
from pipeline.normalize import map_care, norm_phone, norm_street, norm_zip, norm_city
import run_pipeline

app = Flask(__name__)
app.secret_key = "local-review-only"
app.jinja_env.filters["parent"] = parent_label

# One decision at a time: the ledger check, the CRM write and the ledger record must
# not interleave with another request's (a held key or a double click would
# otherwise approve the same item twice).
_decide_lock = threading.Lock()


def load_queue() -> dict:
    if not config.PROPOSALS.exists():
        return {"proposals": [], "confirmed": [], "counts": {}, "parent": {}}
    return json.loads(config.PROPOSALS.read_text())


def open_items(q: dict, ledger: Ledger) -> list[dict]:
    props = [p for p in q["proposals"] if not ledger.is_decided(p["id"])]
    props.sort(key=lambda p: (ORDER.index(p["type"]) if p["type"] in ORDER else 99, -p["confidence"]))
    return props


def groups_for(props: list[dict], confirmed: list[dict]) -> list[dict]:
    """Queue rail: one group per type in reviewer order, confirmed matches last."""
    items = props + confirmed
    return [{"type": t, "label": LABEL[t], "entries": [p for p in items if p["type"] == t]}
            for t in ORDER if any(p["type"] == t for p in items)]


def recon_rows(p: dict, parent: dict) -> list[dict]:
    """Field-aligned worksheet: what the website says, what the CRM has now, what it
    will have after approval. A row is `changed` when the approval touches its field."""
    L, A = p.get("location"), p.get("account")
    ch = {c["field"]: c["to"] for c in p["changes"]}
    t = p["type"]

    def row(field, web, crm, key=None, after=None, note="", differs=False):
        changed = key in ch
        return {"field": field, "web": web, "crm": crm, "differs": differs,
                "after": after if after is not None else (ch[key] if changed else crm), "changed": changed, "note": note}

    if t == "CREATE":
        pl = p["actions"][0]["payload"]
        vals = {**pl, "parent_id": parent_label(parent["name"])}
        return [{"field": f, "web": vals[k], "crm": "", "after": vals[k], "changed": True, "note": ""}
                for f, k in (("Name", "name"), ("Parent", "parent_id"), ("Street", "billing_street"), ("City", "billing_city"),
                             ("State", "billing_state"), ("ZIP", "billing_zip"), ("Care type", "care_type"), ("Phone", "phone"), ("Status", "status"))]

    billing = [row("Lifetime revenue", "", money(A["lifetime_revenue"])), row("Outstanding AR", "", money(A["outstanding_ar"]))]
    if t == "NOT_ON_SITE":
        return [row("Listed on website", "no", "under " + parent_label(A["parent_name"])), row("Status", "", A["status"], key="status")] + billing

    if t == "CHOW":
        # Two accounts after approval: the old one, frozen except for the link, and
        # the successor, which starts with no billing. Show them side by side.
        pl = p["actions"][0]["payload"]
        def pair(field, web, old, new, old_after=None):
            return {"field": field, "web": web, "crm": old, "after": new, "changed": new not in (None, ""), "note": "",
                    "differs": False, "old_after": old_after}
        return [
            pair("Name", L["name"], A["name"], pl["name"]),
            pair("Parent", parent_label(parent["name"]), parent_label(A["parent_name"]), parent_label(parent["name"])),
            pair("Street", L["street"], A["billing_street"], pl["billing_street"]),
            pair("City, state ZIP", f"{L['city']}, {L['state']} {L['zip']}", f"{A['billing_city']}, {A['billing_state']} {A['billing_zip']}",
                 f"{pl['billing_city']}, {pl['billing_state']} {pl['billing_zip']}"),
            pair("Care", ", ".join(L["care_offerings"]), A["care_type"], pl["care_type"]),
            pair("Phone", L["phone"], A["phone"], pl["phone"]),
            pair("Status", "on website", A["status"], pl["status"]),
            pair("Lifetime revenue", "", money(A["lifetime_revenue"]), money(0)),
            pair("Outstanding AR", "", money(A["outstanding_ar"]), money(0)),
            pair("Change of ownership link", "", A["chow_current_account"], "", old_after="the successor's id"),
            pair("Note", "", A["note"], pl["note"]),
        ]

    rows = [row("Name", L["name"], A["name"], key="name", differs=L["name"].strip().casefold() != A["name"].strip().casefold())]
    rows.append(row("Parent", parent_label(parent["name"]), parent_label(A["parent_name"]), key="parent_id",
                    after=parent_label(parent["name"]) if "parent_id" in ch else None, differs=A["parent_id"] != parent.get("account_id")))
    rows += [
        row("Street", L["street"], A["billing_street"], key="billing_street", differs=norm_street(L["street"]) != norm_street(A["billing_street"])),
        row("City, state ZIP", f"{L['city']}, {L['state']} {L['zip']}", f"{A['billing_city']}, {A['billing_state']} {A['billing_zip']}",
            key=next((k for k in ("billing_city", "billing_state", "billing_zip") if k in ch), None),
            after=f"{ch.get('billing_city', A['billing_city'])}, {ch.get('billing_state', A['billing_state'])} {ch.get('billing_zip', A['billing_zip'])}",
            differs=(norm_city(L["city"]) != norm_city(A["billing_city"]) or norm_zip(L["zip"]) != norm_zip(A["billing_zip"])
                     or (L["state"] or "").upper() != (A["billing_state"] or "").upper())),
        row("Care", ", ".join(L["care_offerings"]), A["care_type"], key="care_type",
            differs=bool(map_care(L["care_offerings"])) and A["care_type"] not in map_care(L["care_offerings"])),
        row("Phone", L["phone"], A["phone"], key="phone", differs=bool(L["phone"]) and norm_phone(L["phone"]) != norm_phone(A["phone"])),
        row("Status", "on website", A["status"], key="status"),
    ]
    if t == "DUPLICATE":
        surv = p.get("survivor") or {}
        rows.append(row("Duplicate of", "", A["duplicate_of_account"], key="duplicate_of_account",
                        after=f"{surv.get('name', '')} {ch['duplicate_of_account']}".strip()))
    return rows + billing


def describe_actions(p: dict, parent: dict) -> list[str]:
    """What approving does, in the reviewer's words. One sentence per API call."""
    out = []
    for a in p["actions"]:
        if a["op"] == "create":
            pl = a["payload"]
            out.append(f"Creates a new account \u201c{pl['name']}\u201d under {parent_label(parent['name'])} at {pl['billing_street']}, "
                       f"{pl['billing_city']}, {pl['billing_state']} {pl['billing_zip']} ({pl['care_type'] or 'no care type'}, {pl['phone'] or 'no phone'}).")
        else:
            acct = p["account"] if p["account"] and p["account"]["account_id"] == a["account_id"] else None
            who = f"account {a['account_id']}" + (f" (\u201c{acct['name']}\u201d)" if acct else "")
            fields = []
            for k, v in a["payload"].items():
                if isinstance(v, dict):
                    v = "the new account's id"
                elif k == "parent_id":
                    v = parent_label(parent["name"]) if v == parent.get("account_id") else v
                elif k == "duplicate_of_account" and p.get("survivor"):
                    v = f"{p['survivor']['name']} ({v})"
                before = (acct or {}).get(k)
                fields.append(f"{k} \u2192 {v}" + (f" (was {before})" if before not in (None, "", v) else ""))
            note = " and appends a dated note" if a.get("note_append") else ""
            out.append(f"Updates {who}: " + "; ".join(fields) + note + ".")
    return out


def _render(selected_id: str | None):
    q = load_queue()
    ledger = Ledger()
    props = open_items(q, ledger)
    parent = q.get("parent") or {}
    sel = next((p for p in props + q.get("confirmed", []) if p["id"] == selected_id), None) if selected_id else None
    if selected_id and not sel:
        flash(("ok", "That item has been decided or is no longer in the queue."))
        return redirect(url_for("index"))
    rows = recon_rows(sel, parent) if sel else []
    does = describe_actions(sel, parent) if sel and sel["actions"] else []
    return render_template("review.html", q=q, groups=groups_for(props, q.get("confirmed", [])), total=len(props), sel=sel,
                           rows=rows, does=does, label=LABEL, explain=EXPLAIN, parent=parent, history_count=len(ledger.data))


@app.get("/")
def index():
    props = open_items(load_queue(), Ledger())
    if props:
        return redirect(url_for("show", pid=props[0]["id"]))
    return _render(None)


@app.get("/p/<pid>")
def show(pid):
    return _render(pid)


@app.post("/decide/<pid>")
def decide(pid):
    decision = request.form.get("decision")
    if decision not in ("approve", "reject"):
        abort(400)
    with _decide_lock:
        ledger = Ledger()
        props = open_items(load_queue(), ledger)
        ids = [p["id"] for p in props]
        p = next((x for x in props if x["id"] == pid), None)
        if not p:
            flash(("error", "That item is no longer in the queue (already decided, or the pipeline was re-run)."))
            return redirect(url_for("index"))
        label = f"{LABEL.get(p['type'], p['type'])}: {p['title']}"
        if decision == "reject":
            ledger.record(p, "rejected")
            flash(("ok", f"Rejected. {label}."))
        else:
            result = apply_proposal(p, CRM(), ledger)
            ledger.record(p, "approved", result)
            created = f" Created {result['created_account_id']}." if result.get("created_account_id") else ""
            if result["status"] == "applied":
                flash(("ok", f"Approved. {label}.{created} " + "; ".join(result["log"]) + "."))
            else:
                flash(("error", f"Not written ({result['status']}). {label}. {result['detail']}{created} "
                                f"The item stays in the queue; approving again will reuse any account already created."))
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
