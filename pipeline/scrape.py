"""Scrape every Bellhaven location from the operator website.

Crawl strategy (deliberately broad): the paginated /communities directory is the
main source, but the homepage can link to communities that are not yet in the
directory (e.g. a newly acquired location announced in a banner). So we collect
/communities/<slug> links from the homepage AND every directory page, then
fetch each detail page once for the full address, all care offerings, phone and
administrator.
"""
import html
import http.client
import json
import re
import sys
import time
import urllib.error
import urllib.request

from . import config

_UA = "ltc-ownership-pipeline/1.0 (+reconciliation bot)"
_CARD = re.compile(r'<h3><a href="(/communities/[^"]+)">(.*?)</a></h3>', re.S)
_LINK = re.compile(r'href="(/communities/[a-z0-9\-]+)"')
_BADGE = re.compile(r'<span class="badge">(.*?)</span>', re.S)
_CITY_LINE = re.compile(r"^(.*?),\s*([A-Z]{2})\s+(\d{5})(?:-\d{4})?\s*$")


def _get(path: str) -> str:
    req = urllib.request.Request(config.SITE_BASE + path, headers={"User-Agent": _UA})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            if e.code < 500 or attempt == 2:
                raise
        except (urllib.error.URLError, http.client.HTTPException, OSError):
            if attempt == 2:
                raise
        time.sleep(1.5 * (attempt + 1))


def _text(fragment: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", fragment or "")).strip()


def _field(page: str, label: str) -> str:
    m = re.search(r"<dt>\s*" + re.escape(label) + r"\s*</dt>\s*<dd>(.*?)</dd>", page, re.S)
    return m.group(1) if m else ""


def discover_slugs() -> list[str]:
    """Slugs from the homepage plus every directory page, first-seen order."""
    seen, order = set(), []

    def add(slugs):
        for s in slugs:
            if s not in seen:
                seen.add(s)
                order.append(s)

    add(_LINK.findall(_get("/")))
    prev, page = None, 1
    while page <= 50:
        found = [s for s, _ in _CARD.findall(_get(f"/communities?page={page}"))]
        # The site clamps out-of-range pages to the last page, so stop on an empty
        # page or when a page repeats the previous one. (Not "nothing new": the
        # homepage may already link communities that also appear on page 1.)
        if not found or found == prev:
            break
        add(found)
        prev, page = found, page + 1
    return order


def parse_detail(slug: str, page: str) -> dict:
    m = re.search(r"<h1[^>]*>(.*?)</h1>", page, re.S)
    name = _text(m.group(1)) if m else ""
    if not name:                                   # never let a markup change blank the name
        name = slug.rsplit("/", 1)[-1].replace("-", " ").title()
    addr = _field(page, "Address")
    lines = [_text(x) for x in re.split(r"<br\s*/?>", addr)]
    lines = [l for l in lines if l]
    street = lines[0] if lines else ""
    city = state = zip5 = ""
    if len(lines) > 1:
        m = _CITY_LINE.match(lines[-1])
        if m:
            city, state, zip5 = m.groups()
    care = [html.unescape(b).strip() for b in _BADGE.findall(_field(page, "Care Offerings"))]
    return {
        "slug": slug.rsplit("/", 1)[-1],
        "url": config.SITE_BASE + slug,
        "name": name,
        "street": street,
        "city": city,
        "state": state,
        "zip": zip5,
        "care_offerings": care,
        "phone": _text(_field(page, "Phone")),
        "administrator": _text(_field(page, "Administrator")),
    }


def scrape() -> list[dict]:
    slugs = discover_slugs()
    out = []
    for s in slugs:
        out.append(parse_detail(s, _get(s)))
    return out


def main(argv=None) -> int:
    locs = scrape()
    config.STATE_DIR.mkdir(exist_ok=True)
    config.SITE_SNAPSHOT.write_text(json.dumps(locs, indent=1))
    print(f"scraped {len(locs)} locations -> {config.SITE_SNAPSHOT}")
    missing = [l["name"] for l in locs if not (l["street"] and l["city"] and l["zip"])]
    if missing:
        print("WARNING: incomplete address on:", missing, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
