"""
Safe write test: confirms how the CRM's update (PATCH) behaves.
Every change it makes is put back at the end.

Run:  python api_test.py
"""
from crm import CRM

TEST_ID = "001L3LGR6W69J8DYYV"  # Bellhaven of Maplewood - already correct, needs no changes

crm = CRM()


def try_patch(label, fields):
    try:
        resp = crm.update_account(TEST_ID, fields)
        print(f"[{label}] ACCEPTED -> {resp}")
        return True
    except RuntimeError as e:
        print(f"[{label}] REJECTED -> {e}")
        return False


before = crm.get_account(TEST_ID)
print("BEFORE:", before, "\n")

# 1) Normal write + read back
try_patch("write note", {"note": "api_test.py - temporary, will be restored"})
print("  read back note:", repr(crm.get_account(TEST_ID).get("note")), "\n")

# 2) Restore the original note
try_patch("restore note", {"note": before.get("note", "")})
print()

# 3) Invalid status - does the API protect us?
if try_patch("invalid status", {"status": "NotARealStatus"}):
    try_patch("restore status", {"status": before["status"]})
print()

# 4) Unknown field - rejected or silently ignored?
try_patch("unknown field", {"not_a_real_field": "x"})
print()

after = crm.get_account(TEST_ID)
diffs = {k: (before[k], after.get(k)) for k in before
         if k != "updated_at" and before[k] != after.get(k)}
print("AFTER == BEFORE (ignoring updated_at):", not diffs)
if diffs:
    print("  Differences:", diffs)
