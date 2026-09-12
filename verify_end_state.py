#!/usr/bin/env python3
"""Prove the CRM end state matches the decisions: every applied write landed, CHOW
old accounts are frozen, and every website location resolves to exactly one Active
account under the parent with the same name, phone and zip. Read-only.

    python verify_end_state.py
"""
import json
import sys
from collections import Counter

from pipeline import config
from pipeline.crm import CRM
from pipeline.match import find_parent
from pipeline.normalize import norm_phone, norm_street


def main() -> int:
    crm = CRM().list_accounts()
    by = {a["account_id"]: a for a in crm}
    site = json.loads(config.SITE_SNAPSHOT.read_text())
    dec = json.loads(config.DECISIONS.read_text()) if config.DECISIONS.exists() else {}
    pid = find_parent(crm)["account_id"]
    fails: list[str] = []

    # every applied write landed
    n = 0
    for v in dec.values():
        if v["decision"] != "approved" or v["result"].get("status") != "applied" or v["type"] in ("CONFIRM_MATCH", "CREATE"):
            continue
        a = by[v["account_id"]]
        if v["type"] == "CHOW":
            old_ok = a["parent_id"] != pid and a["status"] == "Active" and a["note"] == "" and a["chow_current_account"] == v["result"]["created_account_id"]
            new = by[v["result"]["created_account_id"]]
            new_ok = new["parent_id"] == pid and new["status"] == "Active" and not new["lifetime_revenue"] and not new["outstanding_ar"]
            if not (old_ok and new_ok):
                fails.append(f"CHOW {v['title']}: old frozen={old_ok}, successor ok={new_ok}")
            n += 1
            continue
        for ch in v["changes"]:
            want = ch["to"]
            got = a["parent_id"] if ch["field"] == "parent_id" else (a.get(ch["field"]) or "")
            if got != (want or ""):
                fails.append(f"{v['type']} {v['title']}: {ch['field']} is {got!r}, expected {want!r}")
            n += 1
        if "[ownership-sync]" not in (a["note"] or ""):
            fails.append(f"{v['type']} {v['title']}: no audit note")
    print(f"applied writes verified: {n}")

    # global invariants
    active = [a for a in crm if a["parent_id"] == pid and a["status"] == "Active"]
    shared = [k for k, c in Counter((norm_street(a["billing_street"]), a["billing_city"].lower()) for a in active).items() if c > 1]
    if shared:
        fails.append(f"Active accounts under the parent share an address: {shared}")
    matched = 0
    for loc in site:
        hits = [a for a in active if norm_street(a["billing_street"]) == norm_street(loc["street"]) and a["billing_city"].lower() == loc["city"].lower()]
        if len(hits) != 1:
            fails.append(f"{loc['name']}: {len(hits)} Active accounts under the parent at its address")
            continue
        a = hits[0]
        if a["name"] != loc["name"] or a["billing_zip"] != loc["zip"] or norm_phone(a["phone"]) != norm_phone(loc["phone"]):
            fails.append(f"{loc['name']}: name/zip/phone differ from the website ({a['name']!r}, {a['billing_zip']}, {a['phone']})")
            continue
        matched += 1
    print(f"website locations with exactly one Active account under the parent, same name/zip/phone: {matched}/{len(site)}")
    dups = [a for a in crm if a["duplicate_of_account"]]
    bad = [a["name"] for a in dups if a["status"] != "Inactive" or by[a["duplicate_of_account"]]["status"] != "Active"]
    if bad:
        fails.append(f"retired duplicates not Inactive or pointing at an inactive survivor: {bad}")
    print(f"retired duplicates: {len(dups)}; Needs Review under the parent: {sum(1 for a in crm if a['parent_id'] == pid and a['status'] == 'Needs Review')}; accounts: {len(crm)}")

    print("RESULT:", "ALL CHECKS PASSED" if not fails else f"{len(fails)} PROBLEM(S)")
    for f in fails:
        print("  -", f)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
