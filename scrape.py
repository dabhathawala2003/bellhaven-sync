"""
Step 1: Scrape every Bellhaven location from the website.

Output: data/website_locations.json and data/website_locations.csv
Run:    python scrape.py

How it finds locations:
  - walks every page of the community directory (follows "Next")
  - ALSO checks the homepage and About page, because a facility can be
    linked there without being in the directory (e.g. a new acquisition)
  - compares the count found against the counts the site itself claims
"""
import csv
import json
import os
import re
import time
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

BASE = "https://analyst-assessment-production.up.railway.app"
LABELS = ["Address", "Care Offerings", "Administrator", "Phone"]
OUT_DIR = "data"

session = requests.Session()
session.headers["User-Agent"] = "bellhaven-sync/1.0"


def get_soup(url):
    """Fetch a page with a few retries, return parsed HTML."""
    for attempt in range(3):
        try:
            r = session.get(url, timeout=30)
            r.raise_for_status()
            return BeautifulSoup(r.text, "html.parser")
        except requests.RequestException:
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))


def community_links(soup):
    """All links on a page that point to a single community page."""
    links = set()
    for a in soup.find_all("a", href=True):
        path = urlparse(urljoin(BASE, a["href"])).path.rstrip("/")
        parts = path.split("/")  # ['', 'communities', '<slug>']
        if len(parts) == 3 and parts[1] == "communities" and parts[2]:
            links.add(BASE + path)
    return links


def discover():
    """Return {community_url: where_we_found_it}."""
    found = {}
    page = 1
    while page <= 50:  # safety limit
        soup = get_soup(f"{BASE}/communities?page={page}")
        links = community_links(soup)
        new = links - set(found)
        for link in new:
            found[link] = f"directory page {page}"
        has_next = soup.find("a", string=re.compile(r"Next", re.I))
        if not new or not has_next:
            break
        page += 1

    for extra in ["/", "/about"]:
        for link in community_links(get_soup(BASE + extra)):
            found.setdefault(link, f"NOT in directory - linked from {extra}")
    return found


def parse_community(url, source):
    """Read one community page and pull out its fields."""
    soup = get_soup(url)
    h1 = soup.find("h1")
    name = h1.get_text(" ", strip=True) if h1 else ""

    # Split page text into lines and group them under each label
    lines = [l.strip() for l in soup.get_text("\n").split("\n") if l.strip()]
    sections, current = {}, None
    for line in lines:
        if line in LABELS:
            current = line
            sections[current] = []
        elif line.startswith("←") or line.startswith("©"):
            current = None
        elif current:
            sections[current].append(line)

    # Address: last line is "City, ST 12345", lines before it are the street
    addr = sections.get("Address", [])
    street = city = state = zip_code = ""
    warnings = []
    if addr:
        m = re.match(r"^(.*?),\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)$", addr[-1])
        if m:
            city, state, zip_code = m.groups()
            street = " ".join(addr[:-1])
        else:
            street = " ".join(addr)
            warnings.append("could not parse city/state/zip")
    else:
        warnings.append("no address found")

    return {
        "slug": url.rsplit("/", 1)[-1],
        "url": url,
        "name": name,
        "street": street,
        "city": city,
        "state": state,
        "zip": zip_code,
        "care_offerings": sections.get("Care Offerings", []),
        "administrator": " ".join(sections.get("Administrator", [])),
        "phone": " ".join(sections.get("Phone", [])),
        "found_via": source,
        "parse_warnings": warnings,
    }


def site_claimed_counts():
    """Numbers the site states about itself, used as a sanity check."""
    home = get_soup(BASE + "/").get_text(" ")
    directory = get_soup(BASE + "/communities").get_text(" ")
    claim_home = re.search(r"serve\s+(\d+)\s+communities", home)
    claim_dir = re.search(r"(\d+)\s+communities listed", directory)
    return (int(claim_home.group(1)) if claim_home else None,
            int(claim_dir.group(1)) if claim_dir else None)


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    links = discover()
    locations = [parse_community(u, s) for u, s in sorted(links.items())]

    with open(os.path.join(OUT_DIR, "website_locations.json"), "w", encoding="utf-8") as f:
        json.dump(locations, f, indent=2)
    with open(os.path.join(OUT_DIR, "website_locations.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(locations[0].keys()))
        w.writeheader()
        for loc in locations:
            row = dict(loc)
            row["care_offerings"] = "; ".join(loc["care_offerings"])
            row["parse_warnings"] = "; ".join(loc["parse_warnings"])
            w.writerow(row)

    # Summary + sanity checks
    home_claim, dir_claim = site_claimed_counts()
    print(f"Scraped {len(locations)} locations")
    print(f"Homepage claims {home_claim}, directory claims {dir_claim}")
    for loc in locations:
        if not loc["found_via"].startswith("directory"):
            print(f"  ! {loc['name']}: {loc['found_via']}")
        if loc["parse_warnings"]:
            print(f"  ! {loc['name']}: {', '.join(loc['parse_warnings'])}")
    if home_claim and len(locations) != home_claim:
        print(f"  ! Count mismatch: found {len(locations)}, homepage says {home_claim}")


if __name__ == "__main__":
    main()
