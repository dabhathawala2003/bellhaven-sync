"""
Step 1: Explore the Bellhaven CRM sandbox (READ ONLY - changes nothing).

Downloads the API spec, your accounts and contacts, and saves them
as JSON files in an "explore" folder so we can study the data.

Before running, set your token (it is never written in this file):
  Windows PowerShell:  $env:CRM_TOKEN="your_token_here"
  Windows cmd:         set CRM_TOKEN=your_token_here
  Mac/Linux:           export CRM_TOKEN=your_token_here
Then run:  python explore.py
"""
import json
import os
import sys

import requests

BASE = "https://analyst-assessment-production.up.railway.app"
TOKEN = os.environ.get("CRM_TOKEN")
OUT = "explore"

if not TOKEN:
    sys.exit("No token found. Set CRM_TOKEN first (see the top of this file).")

os.makedirs(OUT, exist_ok=True)


def save(name, data):
    with open(os.path.join(OUT, name), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


# 1. Download the API spec (the "rulebook") and work out how to send the token
spec = requests.get(f"{BASE}/api/openapi.json", timeout=30).json()
save("openapi.json", spec)

schemes = spec.get("components", {}).get("securitySchemes", {})
print("Auth schemes in the spec:", json.dumps(schemes, indent=2))

headers = {"Authorization": f"Bearer {TOKEN}"}  # common default
for s in schemes.values():
    if s.get("type") == "apiKey" and s.get("in") == "header":
        headers = {s["name"]: TOKEN}
        break

# 2. Check the token works
me = requests.get(f"{BASE}/api/v1/me", headers=headers, timeout=30)
print("\n/me status:", me.status_code)
print(me.text[:500])
if me.status_code != 200:
    sys.exit("\nToken did not work. Paste the output above to Claude (not the token).")

# 3. Download accounts and contacts
for name in ["accounts", "contacts"]:
    r = requests.get(f"{BASE}/api/v1/{name}", headers=headers, timeout=60)
    print(f"\n{name}: status {r.status_code}")
    data = r.json()
    save(f"{name}.json", data)

    # Show the shape of the response so we know about pagination etc.
    if isinstance(data, dict):
        print("  top-level keys:", list(data.keys()))
        items = next((v for v in data.values() if isinstance(v, list)), [])
    else:
        items = data
    print(f"  records in this response: {len(items)}")
    if items:
        print("  first record:")
        print(json.dumps(items[0], indent=2)[:1500])

print(f"\nDone. Files saved in the '{OUT}' folder.")
