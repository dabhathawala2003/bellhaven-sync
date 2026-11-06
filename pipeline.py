"""
Step 3: The daily pipeline (this is what the schedule runs).

  1. scrape the Bellhaven website
  2. take a fresh read-only snapshot of the CRM
  3. run the matcher -> proposals
  4. drop anything already approved/rejected (decision log)
  5. write the review queue: data/queue.json

It NEVER writes to the CRM. Writes only happen in the review app,
one proposal at a time, after a human clicks Approve.

Run:  python pipeline.py              (full run)
      python pipeline.py --no-scrape  (reuse last website scrape)
"""
import json
import os
import sys
from datetime import datetime, timezone

DATA = "data"
DECISIONS = os.path.join(DATA, "decisions.json")
FINAL = ("approved", "rejected")  # decisions that are never re-proposed


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)  # atomic: never leaves a half-written file


# ---------- decision log ----------

def load_decisions():
    return load_json(DECISIONS, {})


def record_decision(pid, decision, reviewer="", comment="", result=None, extra=None):
    decisions = load_decisions()
    entry = decisions.get(pid, {})
    entry.update({"decision": decision, "reviewer": reviewer, "comment": comment,
                  "decided_at": now(), "result": result or []})
    if extra:
        entry.update(extra)
    decisions[pid] = entry
    save_json(DECISIONS, decisions)


def pending(proposals, decisions):
    """Proposals that still need a human. Confirmed matches need nothing."""
    return [p for p in proposals if p["type"] != "confirmed"
            and decisions.get(p["id"], {}).get("decision") not in FINAL]


# ---------- the run ----------

def run(scrape_site=True):
    import scrape
    from crm import snapshot
    from match import Matcher

    os.makedirs(DATA, exist_ok=True)
    if scrape_site:
        scrape.main()
    accounts, contacts = snapshot()
    locations = load_json(os.path.join(DATA, "website_locations.json"), [])
    if not locations:
        sys.exit("No website locations found - refusing to run (would flag everything).")

    # Safety net: if the scrape suddenly finds far fewer locations (site down,
    # layout changed), stop instead of flagging healthy accounts as 'not on website'.
    last = load_json(os.path.join(DATA, "last_run.json"), {})
    if last.get("locations") and len(locations) < 0.8 * last["locations"]:
        sys.exit(f"Scrape found {len(locations)} locations vs {last['locations']} last run - "
                 f"stopping for a human to check the scraper.")

    proposals = Matcher(locations, accounts, contacts).run()
    save_json(os.path.join(DATA, "proposals.json"), proposals)

    decisions = load_decisions()
    queue = pending(proposals, decisions)
    save_json(os.path.join(DATA, "queue.json"), queue)

    save_json(os.path.join(DATA, "last_run.json"),
              {"at": now(), "locations": len(locations), "accounts": len(accounts)})

    actionable = [p for p in proposals if p["type"] != "confirmed"]
    skipped = len(actionable) - len(queue)
    print(f"\n{now()}  findings: {len(proposals)} | need action: {len(actionable)} | "
          f"already decided (skipped): {skipped} | in review queue: {len(queue)}")
    return queue


if __name__ == "__main__":
    run(scrape_site="--no-scrape" not in sys.argv)
