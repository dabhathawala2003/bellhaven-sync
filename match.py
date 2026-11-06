"""
Step 2: Match website locations to CRM accounts and propose changes.

Input:  data/website_locations.json, data/crm_accounts.json, data/crm_contacts.json
Output: data/proposals.json
Run:    python match.py

Nothing here talks to the CRM. It only READS the snapshot files and
WRITES proposals. Changes only happen later, in the review app,
after a human approves them.

The core idea: TRUST THE ADDRESS, NOT THE NAME.
Facilities get renamed after an acquisition, but the building stays put.
"""
import hashlib
import json
import os
import re
from difflib import SequenceMatcher

DATA = "data"
PARENT_NAME = "Bellhaven Senior Living (Parent Account)"

# Website care offerings -> CRM care_type values
CARE_MAP = {
    "Assisted Living": "Assisted Living",
    "Memory Support": "Memory Care",
    "Short-Term Rehabilitation & Nursing": "Skilled Nursing",
}

# Street words that mean the same thing ("Road" == "Rd")
STREET_ABBR = {
    "street": "st", "avenue": "ave", "road": "rd", "boulevard": "blvd",
    "drive": "dr", "lane": "ln", "court": "ct", "place": "pl", "pk": "pike",
    "west": "w", "east": "e", "north": "n", "south": "s",
    "northwest": "nw", "northeast": "ne", "southwest": "sw", "southeast": "se",
}
NAME_ABBR = {"rehab": "rehabilitation", "centre": "center", "&": "and",
             "healthcare": "health care"}


# ---------- normalizing (making messy text comparable) ----------

def norm_street(s):
    s = re.sub(r"[^a-z0-9 ]", " ", (s or "").lower())
    return " ".join(STREET_ABBR.get(t, t) for t in s.split())


def norm_name(s):
    s = (s or "").lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    words = []
    for t in s.split():
        words.extend(NAME_ABBR.get(t, t).split())
    return " ".join(w for w in words if w != "the")


def norm_phone(s):
    return re.sub(r"\D", "", s or "")[-10:]


def name_similarity(a, b):
    return SequenceMatcher(None, norm_name(a), norm_name(b)).ratio()


def is_po_box(street):
    return bool(re.match(r"^\s*p\.?\s*o\.?\s*box", street or "", re.I))


# ---------- helpers ----------

def has_billing_hold(acct):
    """SOP: revenue history AND outstanding AR > 0 -> preserve the old account."""
    return (acct.get("lifetime_revenue") or 0) > 0 and (acct.get("outstanding_ar") or 0) > 0


def is_retired(acct):
    """Accounts we already handled: marked duplicate, or preserved via CHOW."""
    return bool(acct.get("duplicate_of_account") or acct.get("chow_current_account"))


def proposal_id(kind, key, actions):
    """Stable id: same finding on the same data -> same id on every run.
    This is what lets re-runs skip items already decided."""
    raw = json.dumps([kind, key, actions], sort_keys=True)
    return f"{kind}:{key}:{hashlib.sha1(raw.encode()).hexdigest()[:10]}"


def fmt_acct(a):
    return (f"{a['name']} ({a['account_id']}) | parent: {a.get('parent_name') or 'none'} | "
            f"{a['billing_street']}, {a['billing_city']} {a['billing_state']} {a['billing_zip']} | "
            f"rev {a.get('lifetime_revenue', 0):,} / AR {a.get('outstanding_ar', 0):,} | {a['status']}")


def fmt_loc(l):
    return f"{l['name']} | {l['street']}, {l['city']} {l['state']} {l['zip']} | {l['url']}"


# ---------- the matcher ----------

class Matcher:
    def __init__(self, locations, accounts, contacts):
        self.locations = locations
        self.accounts = accounts
        self.parent = next(a for a in accounts if a["name"] == PARENT_NAME)
        self.parent_id = self.parent["account_id"]
        parent_ids = {a["parent_id"] for a in accounts if a.get("parent_id")}
        # Facilities = everything that is not itself a parent company account
        self.facilities = [a for a in accounts if a["account_id"] not in parent_ids
                           and "(Parent Account)" not in a["name"]]
        self.contacts_by_acct = {}
        for c in contacts:
            if c.get("is_active", True):
                self.contacts_by_acct.setdefault(c["account_id"], []).append(c)
        self.proposals = []

    # --- step A: find which CRM accounts are the same building as a location
    def candidates(self, loc):
        """Return (matched_accounts, how_matched, rejected_lookalikes)."""
        live = [a for a in self.facilities if not is_retired(a)]
        same_state = [a for a in live if a["billing_state"] == loc["state"]]

        # 1) Strong: same normalized street + same city
        street_hits = [a for a in same_state
                       if norm_street(a["billing_street"]) == norm_street(loc["street"])
                       and a["billing_city"].lower() == loc["city"].lower()]
        if street_hits:
            return street_hits, "address", []

        # 2) Fallback: same city + near-identical name, and the CRM address is
        #    unusable (PO box / blank) or the phone matches. Handles bad CRM addresses.
        fallback, rejected = [], []
        for a in same_state:
            if a["billing_city"].lower() != loc["city"].lower():
                continue
            sim = name_similarity(a["name"], loc["name"])
            phone_ok = norm_phone(a["phone"]) and norm_phone(a["phone"]) == norm_phone(loc["phone"])
            bad_addr = is_po_box(a["billing_street"]) or not a["billing_street"].strip()
            if sim >= 0.9 and (bad_addr or phone_ok):
                fallback.append(a)
            elif sim >= 0.4:
                rejected.append(a)  # same city, similar name, but a different building
        if fallback:
            return fallback, "name+city (CRM address unusable)", rejected
        return [], "none", rejected

    # --- step B: if several accounts share the building, pick the survivor
    def survivor_rank(self, acct, loc):
        """Higher tuple = better survivor. Order of tie-breakers:
        1 already under Bellhaven, 2 admin on website is a contact here,
        3 has billing history, 4 has contacts, 5 Active, 6 phone matches site,
        7 has no other-company parent, 8 name closest to site, 9 lowest id."""
        contacts = self.contacts_by_acct.get(acct["account_id"], [])
        admin_here = any(c["name"].lower() == loc["administrator"].lower() for c in contacts)
        return (
            acct.get("parent_id") == self.parent_id,
            admin_here,
            (acct.get("lifetime_revenue") or 0) > 0,
            len(contacts) > 0,
            acct["status"] == "Active",
            norm_phone(acct["phone"]) == norm_phone(loc["phone"]),
            not acct.get("parent_id"),
            round(name_similarity(acct["name"], loc["name"]), 3),
            [-ord(ch) for ch in acct["account_id"]],
        )

    # --- step C: decide what to change on the survivor
    def fix_proposal(self, loc, acct, how, evidence):
        fields = {}
        if acct["name"].strip() != loc["name"]:
            fields["name"] = loc["name"]
        if norm_street(acct["billing_street"]) != norm_street(loc["street"]):
            fields["billing_street"] = loc["street"]
        if acct["billing_city"].lower() != loc["city"].lower():
            fields["billing_city"] = loc["city"]
        if acct["billing_state"] != loc["state"]:
            fields["billing_state"] = loc["state"]
        if acct["billing_zip"][:5] != loc["zip"][:5]:
            fields["billing_zip"] = loc["zip"]

        needs_parent = acct.get("parent_id") != self.parent_id
        rev, ar = acct.get("lifetime_revenue") or 0, acct.get("outstanding_ar") or 0
        contacts = self.contacts_by_acct.get(acct["account_id"], [])
        admin_here = any(c["name"].lower() == loc["administrator"].lower() for c in contacts)
        if admin_here:
            evidence.append(f"Website administrator {loc['administrator']} is a contact on this account")

        if not needs_parent and not fields:
            self.add("confirmed", acct["account_id"], loc, acct, [], evidence,
                     "Matches website, no change needed", "high")
            return

        if needs_parent and has_billing_hold(acct):
            # SOP: keep old account untouched, create new one, link with CHOW
            new_fields = {
                "name": loc["name"], "parent_id": self.parent_id,
                "billing_street": loc["street"], "billing_city": loc["city"],
                "billing_state": loc["state"], "billing_zip": loc["zip"],
                "care_type": acct.get("care_type") or care_type_for(loc),
                "phone": acct.get("phone", ""), "status": "Active",
                "note": f"Created via CHOW: Bellhaven acquired from "
                        f"{acct.get('parent_name') or 'unknown owner'}. "
                        f"Previous account {acct['account_id']} preserved for billing.",
            }
            actions = [
                {"op": "create", "ref": "NEW", "fields": new_fields},
                {"op": "update", "account_id": acct["account_id"],
                 "fields": {"chow_current_account": "$NEW"}},
            ]
            evidence.append(f"Billing hold: revenue {rev:,} AND outstanding AR {ar:,} > 0 "
                            f"-> SOP says preserve old account, create new + CHOW link")
            self.add("chow", acct["account_id"], loc, acct, actions, evidence,
                     f"Ownership change {acct.get('parent_name') or 'none'} -> Bellhaven (CHOW path)",
                     "high" if how == "address" else "medium")
            return

        if needs_parent:
            fields["parent_id"] = self.parent_id
            reason = ("no outstanding AR" if rev > 0 else "no revenue history")
            evidence.append(f"Re-parent directly: revenue {rev:,}, AR {ar:,} ({reason})")
        changed = ", ".join(k for k in fields)
        kind = "reparent" if needs_parent else "update"
        self.add(kind, acct["account_id"], loc, acct,
                 [{"op": "update", "account_id": acct["account_id"], "fields": fields}],
                 evidence, f"Update {changed}", "high" if how == "address" else "medium")

    def add(self, kind, key, loc, acct, actions, evidence, summary, confidence):
        self.proposals.append({
            "id": proposal_id(kind, key, actions),
            "type": kind,
            "summary": summary,
            "confidence": confidence,
            "location": loc,
            "account": acct,
            "actions": actions,
            "evidence": evidence,
        })

    # --- run everything
    def run(self):
        matched_ids = set()
        for loc in self.locations:
            hits, how, rejected = self.candidates(loc)
            evidence = [f"Website: {fmt_loc(loc)}"]
            if loc.get("found_via", "").startswith("NOT in directory"):
                evidence.append(f"Found via homepage, not the directory ({loc['found_via']})")
            for r in rejected:
                evidence.append(f"Considered and REJECTED (different building): {fmt_acct(r)}")

            if not hits:
                self.add("create", loc["slug"], loc, None,
                         [{"op": "create", "ref": "NEW", "fields": {
                             "name": loc["name"], "parent_id": self.parent_id,
                             "billing_street": loc["street"], "billing_city": loc["city"],
                             "billing_state": loc["state"], "billing_zip": loc["zip"],
                             "care_type": care_type_for(loc), "phone": loc["phone"],
                             "status": "Active",
                             "note": f"Created from Bellhaven website listing {loc['url']}"}}],
                         evidence + ["No CRM account at this address"],
                         "Location on website has no CRM account", "high")
                continue

            hits.sort(key=lambda a: self.survivor_rank(a, loc), reverse=True)
            survivor, losers = hits[0], hits[1:]
            matched_ids.update(a["account_id"] for a in hits)
            evidence.append(f"Matched by {how}: {fmt_acct(survivor)}")
            self.fix_proposal(loc, survivor, how, list(evidence))

            for loser in losers:
                ev = [f"Website: {fmt_loc(loc)}",
                      f"Survivor: {fmt_acct(survivor)}",
                      f"Duplicate: {fmt_acct(loser)}",
                      "Same building (same normalized street + city); survivor chosen by: "
                      "under Bellhaven > admin contact > billing history > contacts > Active"]
                n = len(self.contacts_by_acct.get(loser["account_id"], []))
                if n:
                    ev.append(f"Note: duplicate has {n} active contact(s) - not moved automatically")
                if has_billing_hold(loser):
                    ev.append("WARNING: duplicate has revenue + AR; billing should confirm")
                self.add("duplicate", loser["account_id"], loc, loser,
                         [{"op": "update", "account_id": loser["account_id"], "fields": {
                             "duplicate_of_account": survivor["account_id"],
                             "status": "Inactive",
                             "note": f"Duplicate of {survivor['account_id']} "
                                     f"({loc['name']}), same address {loc['street']}, {loc['city']}"}}],
                         ev, f"Duplicate of {survivor['name']}",
                         "high" if not has_billing_hold(loser) else "medium")

        self.unlisted(matched_ids)
        return self.proposals

    # --- step D: Bellhaven accounts that are no longer on the website
    def unlisted(self, matched_ids):
        for acct in self.facilities:
            if (acct.get("parent_id") != self.parent_id or acct["account_id"] in matched_ids
                    or is_retired(acct) or acct["status"] in ("Inactive", "Needs Review")):
                continue
            ev = [f"CRM: {fmt_acct(acct)}",
                  "Not listed on the Bellhaven website (directory + homepage + about page)"]
            # Is there another live account at the same building under another owner?
            new_owner = [a for a in self.facilities
                         if a["account_id"] != acct["account_id"] and not is_retired(a)
                         and a.get("parent_id") and a.get("parent_id") != self.parent_id
                         and norm_street(a["billing_street"]) == norm_street(acct["billing_street"])
                         and a["billing_city"].lower() == acct["billing_city"].lower()]
            if new_owner:
                cur = new_owner[0]
                ev.append(f"Same building now has an account under another owner: {fmt_acct(cur)}")
                if has_billing_hold(acct):
                    ev.append("Billing hold (revenue + AR > 0): keep old account as-is, "
                              "link CHOW to the EXISTING new-owner account instead of "
                              "creating a second duplicate")
                    actions = [{"op": "update", "account_id": acct["account_id"],
                                "fields": {"chow_current_account": cur["account_id"]}}]
                    self.add("chow_existing", acct["account_id"], None, acct, actions, ev,
                             f"Sold to {cur.get('parent_name')} - CHOW link to existing account",
                             "medium")
                else:
                    actions = [{"op": "update", "account_id": acct["account_id"], "fields": {
                        "duplicate_of_account": cur["account_id"], "status": "Inactive",
                        "note": f"No longer Bellhaven; current account is {cur['account_id']}"}}]
                    self.add("duplicate", acct["account_id"], None, acct, actions, ev,
                             f"Stale Bellhaven record; current owner {cur.get('parent_name')}",
                             "medium")
            else:
                ev.append("No other account found at this address - owner unknown")
                actions = [{"op": "update", "account_id": acct["account_id"], "fields": {
                    "status": "Needs Review",
                    "note": "Not on Bellhaven website as of last sync; ownership unverified. "
                            "Parent left unchanged pending confirmation."}}]
                self.add("not_on_website", acct["account_id"], None, acct, actions, ev,
                         "Under Bellhaven in CRM but not on website", "low")


def care_type_for(loc):
    for offering in loc.get("care_offerings", []):
        if offering in CARE_MAP:
            return CARE_MAP[offering]
    return ""


def load(name):
    with open(os.path.join(DATA, name), encoding="utf-8") as f:
        return json.load(f)


def main():
    m = Matcher(load("website_locations.json"), load("crm_accounts.json"),
                load("crm_contacts.json"))
    proposals = m.run()
    with open(os.path.join(DATA, "proposals.json"), "w", encoding="utf-8") as f:
        json.dump(proposals, f, indent=2)

    counts = {}
    for p in proposals:
        counts[p["type"]] = counts.get(p["type"], 0) + 1
    print(f"{len(proposals)} findings:", ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    for p in proposals:
        if p["type"] != "confirmed":
            # For duplicates show the account being retired, otherwise the website name
            src_rec = p["account"] if p["type"] == "duplicate" or not p["location"] else p["location"]
            name = src_rec["name"]
            print(f"  [{p['type']:<14}] {name:<50} {p['summary']}")


if __name__ == "__main__":
    main()
