#!/usr/bin/env python3
"""Daily entry point: scrape -> snapshot CRM -> match -> drop already-decided -> write queue.

    python run_pipeline.py            # full run
    python run_pipeline.py --no-scrape  # reuse the last website snapshot

Never writes to the CRM. Approvals happen in review_app.py.
"""
import argparse
import datetime as dt
import json
import sys
from collections import Counter

from pipeline import config
from pipeline.crm import CRM
from pipeline.ledger import Ledger
from pipeline.match import build_proposals
from pipeline.scrape import scrape


def run(no_scrape: bool = False) -> dict:
    config.STATE_DIR.mkdir(exist_ok=True)
    if no_scrape and config.SITE_SNAPSHOT.exists():
        site = json.loads(config.SITE_SNAPSHOT.read_text())
    else:
        site = scrape()
        config.SITE_SNAPSHOT.write_text(json.dumps(site, indent=1))
    crm = CRM().list_accounts()
    config.CRM_SNAPSHOT.write_text(json.dumps(crm, indent=1))

    out = build_proposals(site, crm)
    ledger = Ledger()
    raw = out["proposals"]
    out["proposals"] = [p for p in raw if not ledger.is_decided(p["id"])]
    out["suppressed"] = len(raw) - len(out["proposals"])
    out["generated_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    out["counts"] = {"site_locations": len(site), "crm_accounts": len(crm), "confirmed": len(out["confirmed"]),
                     "by_type": dict(Counter(p["type"] for p in out["proposals"]))}
    config.PROPOSALS.write_text(json.dumps(out, indent=1))
    with config.RUN_LOG.open("a") as f:
        f.write(json.dumps({"at": out["generated_at"], **out["counts"], "suppressed_already_decided": out["suppressed"]}) + "\n")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-scrape", action="store_true")
    a = ap.parse_args(argv)
    out = run(no_scrape=a.no_scrape)
    c = out["counts"]
    print(f"site locations: {c['site_locations']}   crm accounts: {c['crm_accounts']}   parent: {out['parent']['name']}")
    print(f"confirmed (no action): {c['confirmed']}   suppressed (already decided): {out['suppressed']}")
    print(f"open proposals: {len(out['proposals'])}")
    for t, n in sorted(c["by_type"].items(), key=lambda kv: -kv[1]):
        print(f"  {t:18s} {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
