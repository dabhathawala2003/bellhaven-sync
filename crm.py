"""
CRM helper: one place for all API calls.

Other files use it like:
    from crm import CRM
    crm = CRM()
    accounts = crm.get_all("accounts")

Run it directly to download a READ-ONLY snapshot of the CRM:
    python crm.py
Output: data/crm_accounts.json and data/crm_contacts.json
"""
import json
import os
import sys
import time

import requests

BASE = "https://analyst-assessment-production.up.railway.app/api/v1"


class CRM:
    def __init__(self, token=None):
        token = token or os.environ.get("CRM_TOKEN")
        if not token:
            sys.exit("No token found. In PowerShell run: $env:CRM_TOKEN=\"your_token\"")
        self.s = requests.Session()
        self.s.headers.update({
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        })

    def _request(self, method, path, **kwargs):
        """Send a request, retry a few times on network/server errors."""
        for attempt in range(3):
            try:
                r = self.s.request(method, f"{BASE}{path}", timeout=60, **kwargs)
                if r.status_code >= 500 and attempt < 2:
                    time.sleep(2 * (attempt + 1))
                    continue
                if not r.ok:
                    raise RuntimeError(f"{method} {path} failed {r.status_code}: {r.text[:500]}")
                return r.json()
            except requests.RequestException:
                if attempt == 2:
                    raise
                time.sleep(2 * (attempt + 1))

    def get_all(self, resource):
        """Read every record, page by page, until we have 'total' records."""
        records, page = [], 1
        while True:
            resp = self._request("GET", f"/{resource}", params={"page": page})
            batch = resp.get("data", [])
            records.extend(batch)
            total = resp.get("total", len(records))
            if not batch or len(records) >= total or page > 100:
                break
            page += 1
        return records

    def search_accounts(self, **params):
        """Filtered list, e.g. search_accounts(q="Bellhaven of Tiffin", city="Tiffin")."""
        params.setdefault("page_size", 50)
        return self._request("GET", "/accounts", params=params).get("data", [])

    def get_account(self, account_id):
        return self._request("GET", f"/accounts/{account_id}")

    def update_account(self, account_id, fields):
        """Change only the given fields on one account."""
        return self._request("PATCH", f"/accounts/{account_id}", json=fields)

    def create_account(self, fields):
        return self._request("POST", "/accounts", json=fields)


def snapshot(crm=None):
    """Save every account and contact to data/ (read only). Returns (accounts, contacts)."""
    crm = crm or CRM()
    os.makedirs("data", exist_ok=True)
    out = {}
    for resource in ["accounts", "contacts"]:
        rows = crm.get_all(resource)
        path = os.path.join("data", f"crm_{resource}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(rows, f, indent=2)
        key = "account_id" if resource == "accounts" else "contact_id"
        print(f"{resource}: {len(rows)} records ({len({r[key] for r in rows})} unique ids) -> {path}")
        out[resource] = rows
    return out["accounts"], out["contacts"]


if __name__ == "__main__":
    snapshot()
