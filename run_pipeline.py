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
    client = CRM()                                   # fail fast on a missing token, before crawling
    previous = json.loads(config.SITE_SNAPSHOT.read_text()) if config.SITE_SNAPSHOT.exists() else []
    if no_scrape and previous:
        site = previous
    else:
        site = scrape()
        # A maintenance page or a markup change must not turn into "every account
        # is not on the website": refuse to publish a crawl far smaller than the last.
        floor = max(1, len(previous) // 2)
        if len(site) < floor:
            raise RuntimeError(f"scrape returned {len(site)} locations but the previous snapshot had {len(previous)}; "
                               f"refusing to publish a queue from a suspicious crawl")
        config.SITE_SNAPSHOT.write_text(json.dumps(site, indent=1))
    incomplete = [l["name"] for l in site if not all(l.get(k) for k in ("street", "city", "state", "zip"))]
    if incomplete:
        print(f"WARNING: address did not parse for {incomplete}; address fixes are not proposed for them", file=sys.stderr)

    crm = client.list_accounts()
    config.CRM_SNAPSHOT.write_text(json.dumps(crm, indent=1))

    ledger = Ledger()
    out = build_proposals(site, crm, links=ledger.match_links())
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
    try:
        out = run(no_scrape=a.no_scrape)
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    c = out["counts"]
    print(f"site locations: {c['site_locations']}   crm accounts: {c['crm_accounts']}   parent: {out['parent']['name']}")
    print(f"confirmed (no action): {c['confirmed']}   suppressed (already decided): {out['suppressed']}")
    print(f"open proposals: {len(out['proposals'])}")
    for t, n in sorted(c["by_type"].items(), key=lambda kv: -kv[1]):
        print(f"  {t:18s} {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
