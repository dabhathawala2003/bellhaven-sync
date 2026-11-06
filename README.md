# Bellhaven CRM Sync

Keeps the CRM's picture of **which facilities Bellhaven Senior Living owns** in line with
Bellhaven's own website. It scrapes the website, compares it to the CRM, proposes fixes with
evidence, and only writes to the CRM after a human approves each change.

## How it works

```
scrape.py   -> every location on the website (directory pages + homepage + about page)
crm.py      -> read-only snapshot of all CRM accounts and contacts (all pages)
match.py    -> links each location to CRM accounts, classifies, builds proposals + evidence
pipeline.py -> runs the three steps above daily, skips already-decided items -> review queue
app.py      -> Streamlit review app: current vs proposed, evidence, Approve / Reject
apply.py    -> the ONLY code that writes, called on Approve, with safety checks
```

## Matching approach

**Trust the address, not the name.** Facilities get renamed after acquisitions, but the building
stays put.

1. Normalize streets ("1120 West Main Street" = "1120 W Main St") and match on **street + city +
   state**. This catches renamed facilities (Sunny Acres Retirement Home is Bellhaven Willow Creek).
2. Fallback for unusable CRM addresses (PO box / blank): same city + near-identical name, and the
   PO box case is confirmed by the phone number (Ashtabula).
3. Similar-looking accounts in the same city but at a different address are shown as
   **"considered and rejected"** evidence (Union Square Senior Living is not Bellhaven at Union Square).
   Same-name accounts in other states are never matched (Amberly Manor CO vs Amberly Manor OH).
4. If several accounts share one building, a fixed tie-break picks the survivor: already under
   Bellhaven, then website administrator is a contact, then billing history, then has contacts,
   then Active, then phone match. The rest become duplicates.
5. The reverse check: Bellhaven accounts that are not on the website. If another owner has an
   account at the same address, the facility was sold (Sandusky to Millstone). If not, it is
   marked Needs Review with a note; the parent is not changed without evidence.

Phone numbers often differ between the website and CRM for the same building, so phone is
supporting evidence only, never the deciding signal.

## The billing SOP (change of ownership)

Before any parent change, the account's `lifetime_revenue` and `outstanding_ar` are checked:

- **Revenue > 0 AND AR > 0** -> the old account is left untouched, a new account is created under
  the correct parent, and `chow_current_account` on the old account points to it
  (Marietta, Tiffin).
- **Otherwise** -> the existing account is re-parented directly (Lima, Findlay, Zanesville, Kettering).

The check runs twice: once in the matcher, and again on the **live** account right before writing,
so a change in AR between the pipeline run and the approval cannot slip through.

Judgment call: **Sandusky** was sold by Bellhaven, has a billing hold, and a correct account under
the new owner already exists. Creating another new account would add a duplicate, so the old
account is linked via `chow_current_account` to the existing Millstone account instead.

## Safety

- The pipeline never writes. Writes happen one proposal at a time, only on Approve.
- **Stale check:** before writing, the account is re-read; if it changed since the proposal was
  made, the write is refused.
- **Read-back verification:** every field written is read back and compared.
- **No duplicate creates:** before creating, the CRM is searched for an account with the same name,
  parent and street. For CHOW, the new id is saved the moment it exists, so a retry after a
  half-finished write resumes instead of creating a second account.
- **Scrape guard:** if the scrape finds far fewer locations than last run (site down or layout
  change), the pipeline stops rather than flagging healthy accounts as "not on website".
- Before building the app, `api_test.py` confirmed the API's mutable fields and validation on one
  account and restored it exactly.

## Safe re-runs

- Every proposal has a **stable id** (a hash of what it proposes). Approve/Reject decisions are
  stored in `data/decisions.json`, and decided ids are never queued again.
- The pipeline always reads the **live** CRM, so applied fixes stop showing up as problems.
- If the underlying data changes later, the proposal gets a new id and is reviewed again.

Tested by simulating every approval on a copy of the CRM and re-running the matcher:
35 of 35 website locations confirmed, 0 new proposals.

## Results on my CRM copy

35 website locations, 121 CRM accounts. 29 proposals: 28 approved, 1 rejected.

| Outcome | Count |
|---|---|
| Already correct | 16 |
| Name / address fixes | 9 (1 rejected as cosmetic: Arbors Dayton, only "The" and a hyphen differ) |
| Re-parented directly | 4 |
| CHOW: new account + link | 2 |
| CHOW link to existing new-owner account | 1 |
| New accounts created | 4 |
| Duplicates marked Inactive with `duplicate_of_account` | 7 |
| Not on website -> Needs Review | 2 |

## Run it

```
pip install -r requirements.txt
$env:CRM_TOKEN="your_token"          # PowerShell; never put the token in code
python pipeline.py                   # scrape -> snapshot -> match -> queue (read only)
python -m streamlit run app.py       # review app at http://localhost:8501
```

## Schedule

- `.github/workflows/daily-sync.yml` runs the pipeline daily at 11:00 UTC with the token from a
  GitHub secret and saves the review queue as an artifact. `data/decisions.json` is committed, so
  scheduled runs skip decided items.
- `cron.txt` has the equivalent crontab line for a server.

## What I'd build next

- Move contacts from duplicate accounts to the survivor, and flag administrator changes
  (several website administrators differ from CRM contacts).
- Skip cosmetic-only renames automatically (case, "The", punctuation).
- Store decisions in a shared database instead of a JSON file, so several reviewers can work the
  queue, and post a daily Slack summary of new proposals.
- Run the same pipeline for other parent companies, using each operator's website.
