"""Thin client for the CRM sandbox API. Stdlib only."""
import http.client
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config

MUTABLE_FIELDS = {
    "name", "parent_id", "status", "note", "care_type", "phone",
    "billing_street", "billing_city", "billing_state", "billing_zip",
    "chow_current_account", "duplicate_of_account",
}


class CRMError(RuntimeError):
    pass


class CRM:
    def __init__(self, base: str = config.API_BASE, token: str = config.TOKEN):
        if not token:
            raise CRMError("CRM_TOKEN is not set (see .env.example)")
        self.base = base
        self.token = token

    def _request(self, method: str, path: str, params: dict | None = None, body: dict | None = None):
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Content-Type", "application/json")
        # POST is never retried: a lost response after the origin committed the insert
        # would otherwise create the same account twice. Everything else is safe to retry.
        retry_ok = method != "POST"
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    return json.loads(r.read().decode())
            except urllib.error.HTTPError as e:
                detail = f"{e.code}: {e.read().decode(errors='replace')}"
                if retry_ok and e.code >= 500 and attempt < 2:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise CRMError(f"{method} {path} -> {detail}") from None
            except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as e:
                # URLError: could not connect or send. HTTPException/OSError: the connection
                # dropped or timed out while READING the response (urllib does not wrap
                # those). ValueError: the body was not JSON.
                if retry_ok and attempt < 2:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise CRMError(f"{method} {path} -> {type(e).__name__}: {e}") from None

    # -- reads ---------------------------------------------------------------
    def me(self):
        return self._request("GET", "/me")

    def list_accounts(self, **filters) -> list[dict]:
        """Return every account matching filters, following pagination."""
        out, page = [], 1
        while True:
            r = self._request("GET", "/accounts", params={"page": page, "page_size": 200, **filters})
            out.extend(r["data"])
            if len(out) >= r.get("total", 0) or not r["data"]:
                return out
            page += 1

    def get_account(self, account_id: str) -> dict:
        return self._request("GET", f"/accounts/{account_id}")

    # -- writes (only ever called from apply.py after a human approval) -------
    def create_account(self, payload: dict) -> dict:
        return self._request("POST", "/accounts", body=payload)

    def update_account(self, account_id: str, payload: dict) -> dict:
        bad = set(payload) - MUTABLE_FIELDS
        if bad:
            raise CRMError(f"refusing to PATCH non-mutable fields: {sorted(bad)}")
        return self._request("PATCH", f"/accounts/{account_id}", body=payload)
