"""
Applies ONE approved proposal to the CRM, safely.

Safety checks, in order:
  1. STALE CHECK  - re-read the account live. If someone changed it since the
                    proposal was made, stop (the evidence may be out of date).
  2. SOP RE-CHECK - if we're about to change parent_id, re-check revenue/AR on
                    the LIVE account. If it now has a billing hold, stop.
  3. WRITE        - create / update through the API.
  4. VERIFY       - read the account back and confirm every field landed.
  5. RESUME       - for CHOW (create + link), the new account id is saved right
                    after creation, so a retry never creates a second copy.
"""
from match import has_billing_hold, norm_street

# Fields whose change would make a proposal's evidence out of date
WATCH = ["name", "parent_id", "status", "billing_street", "billing_city", "billing_state",
         "billing_zip", "lifetime_revenue", "outstanding_ar",
         "chow_current_account", "duplicate_of_account"]


class ApplyError(Exception):
    pass


def extract_id(resp):
    """The create endpoint's response shape isn't documented, so look carefully."""
    if isinstance(resp, dict):
        for key in ("account_id", "id"):
            if resp.get(key):
                return resp[key]
        for nested in ("data", "account"):
            if isinstance(resp.get(nested), dict):
                found = extract_id(resp[nested])
                if found:
                    return found
    return None


def verify(crm, account_id, fields):
    live = crm.get_account(account_id)
    bad = {k: (v, live.get(k)) for k, v in fields.items() if str(live.get(k, "")) != str(v)}
    if bad:
        raise ApplyError(f"Read-back mismatch on {account_id}: {bad}")
    return live


def find_existing(crm, fields):
    """Was this account already created (e.g. an earlier attempt that crashed)?
    Same name + same parent + same street = same account -> reuse, don't duplicate."""
    hits = crm.search_accounts(q=fields["name"], city=fields.get("billing_city", ""))
    for a in hits:
        if (a["name"] == fields["name"] and a.get("parent_id") == fields.get("parent_id")
                and norm_street(a["billing_street"]) == norm_street(fields.get("billing_street", ""))):
            return a["account_id"]
    return None


def stale_check(crm, proposal):
    snap = proposal.get("account")
    if not snap:
        return None
    live = crm.get_account(snap["account_id"])
    changed = {k: (snap.get(k), live.get(k)) for k in WATCH if snap.get(k) != live.get(k)}
    if changed:
        raise ApplyError(f"Account changed since this proposal was made: {changed}. "
                         f"Re-run the pipeline to get fresh proposals.")
    return live


def apply_proposal(crm, proposal, prior_new_id=None, on_created=None):
    """Returns (log_lines, new_account_id_or_None). Raises ApplyError on any problem."""
    log = []
    live = stale_check(crm, proposal)
    if live:
        log.append(f"Stale check OK: {live['name']} unchanged since snapshot")

    new_id = prior_new_id
    for action in proposal["actions"]:
        if action["op"] == "create":
            if new_id:
                log.append(f"Create skipped - already created earlier as {new_id} (resume)")
                continue
            existing = find_existing(crm, action["fields"])
            if existing:
                new_id = existing
                if on_created:
                    on_created(new_id)
                log.append(f"Create skipped - matching account {new_id} already exists (reused)")
                continue
            resp = crm.create_account(action["fields"])
            new_id = extract_id(resp) or find_existing(crm, action["fields"])
            if not new_id:
                raise ApplyError(f"Create returned no account id and none found by search: {resp}")
            if on_created:
                on_created(new_id)  # save immediately so a retry won't duplicate
            check = {k: v for k, v in action["fields"].items() if k != "note"}
            verify(crm, new_id, check)
            log.append(f"Created {new_id} ({action['fields']['name']}) - verified")

        elif action["op"] == "update":
            fields = {k: (new_id if v == "$NEW" else v) for k, v in action["fields"].items()}
            if "$NEW" in action["fields"].values() and not new_id:
                raise ApplyError("Link step needs the new account id, but none exists")
            if "parent_id" in fields:
                acct = crm.get_account(action["account_id"])
                if has_billing_hold(acct):
                    raise ApplyError(
                        f"SOP block: {acct['name']} has revenue {acct['lifetime_revenue']:,} and "
                        f"AR {acct['outstanding_ar']:,}. Parent must not change - needs CHOW path.")
            crm.update_account(action["account_id"], fields)
            verify(crm, action["account_id"], fields)
            log.append(f"Updated {action['account_id']}: {', '.join(fields)} - verified")
        else:
            raise ApplyError(f"Unknown action {action['op']}")
    return log, new_id
