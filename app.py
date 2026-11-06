"""
Step 4: The review app.

Run:  streamlit run app.py
Opens in your browser at http://localhost:8501

Each proposal shows: what changes (current vs proposed), the evidence,
and the billing numbers. Approve -> writes to the CRM with safety checks.
Reject -> recorded, never proposed again. Nothing writes without a click.
"""
import os

import streamlit as st

from apply import ApplyError, apply_proposal
from crm import CRM
from pipeline import DATA, load_decisions, load_json, pending, record_decision, run

st.set_page_config(page_title="Bellhaven CRM Sync - Review", layout="wide")

ORDER = ["chow", "chow_existing", "reparent", "create", "duplicate", "update", "not_on_website"]
LABEL = {
    "chow": "🛑 Ownership change - CHOW (billing hold)",
    "chow_existing": "🛑 Sold - CHOW link to existing account",
    "reparent": "🔀 Move to Bellhaven parent",
    "create": "➕ New account",
    "duplicate": "👯 Duplicate",
    "update": "✏️ Update details",
    "not_on_website": "❓ Not on website",
}


@st.cache_resource
def get_crm():
    return CRM()


def title_of(p):
    if p["type"] == "duplicate" or not p.get("location"):
        return p["account"]["name"]
    return p["location"]["name"]


def change_rows(p):
    """Current vs proposed, one row per field, for every action."""
    rows = []
    acct = p.get("account") or {}
    for a in p["actions"]:
        if a["op"] == "create":
            for k, v in a["fields"].items():
                rows.append({"step": "create NEW account", "field": k, "current": "-", "proposed": v})
        else:
            target = a["account_id"]
            label = f"update {target}" + (" (this account)" if target == acct.get("account_id") else "")
            for k, v in a["fields"].items():
                current = acct.get(k, "") if target == acct.get("account_id") else "?"
                shown = "<id of NEW account>" if v == "$NEW" else v
                rows.append({"step": label, "field": k, "current": current, "proposed": shown})
    return rows


def flash(kind, text):
    st.session_state["flash"] = (kind, text)


# ---------------- sidebar ----------------
with st.sidebar:
    st.header("Bellhaven CRM Sync")
    reviewer = st.text_input("Reviewer", value="Drashtant")
    skip_scrape = st.checkbox("Reuse last website scrape", value=False)
    if st.button("▶ Run pipeline now", use_container_width=True):
        with st.spinner("Scraping website, reading CRM, matching..."):
            run(scrape_site=not skip_scrape)
        flash("success", "Pipeline finished - queue refreshed.")
        st.rerun()

    last = load_json(os.path.join(DATA, "last_run.json"), {})
    if last:
        st.caption(f"Last run: {last['at']} | {last['locations']} website locations, "
                   f"{last['accounts']} CRM accounts")

proposals = load_json(os.path.join(DATA, "proposals.json"), [])
decisions = load_decisions()
queue = sorted(pending(proposals, decisions),
               key=lambda p: (ORDER.index(p["type"]) if p["type"] in ORDER else 99, title_of(p)))

with st.sidebar:
    approved = sum(1 for d in decisions.values() if d["decision"] == "approved")
    rejected = sum(1 for d in decisions.values() if d["decision"] == "rejected")
    c1, c2, c3 = st.columns(3)
    c1.metric("Pending", len(queue))
    c2.metric("Approved", approved)
    c3.metric("Rejected", rejected)
    types = st.multiselect("Show types", ORDER, default=ORDER, format_func=lambda t: LABEL[t])

# ---------------- main ----------------
st.title("Review queue")
if "flash" in st.session_state:
    kind, text = st.session_state.pop("flash")
    getattr(st, kind)(text)

if not proposals:
    st.info("No proposals yet - click **Run pipeline now** in the sidebar.")
    st.stop()

tab_queue, tab_history, tab_ok = st.tabs(
    [f"Queue ({len(queue)})", f"History ({len(decisions)})",
     f"Confirmed matches ({sum(p['type'] == 'confirmed' for p in proposals)})"])

with tab_queue:
    shown = [p for p in queue if p["type"] in types]
    if not shown:
        st.success("Nothing to review. 🎉")
    for p in shown:
        pid = p["id"]
        with st.container(border=True):
            st.subheader(f"{LABEL.get(p['type'], p['type'])}: {title_of(p)}")
            st.caption(f"{p['summary']}  |  confidence: **{p['confidence']}**  |  id: `{pid}`")

            acct = p.get("account")
            if acct and p["type"] in ("chow", "chow_existing", "reparent"):
                rev, ar = acct.get("lifetime_revenue", 0), acct.get("outstanding_ar", 0)
                hold = rev > 0 and ar > 0
                msg = (f"Billing check: lifetime revenue **{rev:,}**, outstanding AR **{ar:,}** -> "
                       + ("**billing hold**: old account preserved, CHOW link used."
                          if hold else "no hold: parent can change directly."))
                (st.warning if hold else st.info)(msg)

            st.markdown("**Proposed changes**")
            st.dataframe(change_rows(p), use_container_width=True, hide_index=True)

            st.markdown("**Evidence**")
            st.markdown("\n".join(f"- {e}" for e in p["evidence"]))

            prior = decisions.get(pid, {})
            if prior.get("decision") == "failed":
                st.error(f"Last attempt failed: {prior.get('comment')}")
            if prior.get("new_id"):
                st.caption(f"Resume: new account {prior['new_id']} was already created.")

            comment = st.text_input("Comment (optional)", key=f"c_{pid}")
            b1, b2, _ = st.columns([1, 1, 4])
            if b1.button("✅ Approve", key=f"a_{pid}", type="primary"):
                try:
                    log, new_id = apply_proposal(
                        get_crm(), p, prior_new_id=prior.get("new_id"),
                        on_created=lambda i: record_decision(
                            pid, "in_progress", reviewer, comment, extra={"new_id": i}))
                    record_decision(pid, "approved", reviewer, comment, log,
                                    extra={"new_id": new_id} if new_id else None)
                    flash("success", f"Approved & written: {title_of(p)}\n\n" + "\n".join(log))
                except (ApplyError, RuntimeError) as e:
                    record_decision(pid, "failed", reviewer, str(e),
                                    extra={"new_id": load_decisions().get(pid, {}).get("new_id")})
                    flash("error", f"Not applied: {title_of(p)} - {e}")
                st.rerun()
            if b2.button("❌ Reject", key=f"r_{pid}"):
                record_decision(pid, "rejected", reviewer, comment)
                flash("info", f"Rejected: {title_of(p)} (won't be proposed again)")
                st.rerun()

with tab_history:
    by_id = {p["id"]: p for p in proposals}
    rows = []
    for pid, d in sorted(decisions.items(), key=lambda kv: kv[1].get("decided_at", ""), reverse=True):
        p = by_id.get(pid)
        rows.append({"when": d.get("decided_at"), "decision": d["decision"],
                     "item": title_of(p) if p else pid, "type": p["type"] if p else "",
                     "reviewer": d.get("reviewer"), "comment": d.get("comment"),
                     "result": " | ".join(d.get("result", []))})
    st.dataframe(rows, use_container_width=True, hide_index=True)

with tab_ok:
    st.dataframe([{"website": p["location"]["name"], "crm account": p["account"]["account_id"]}
                  for p in proposals if p["type"] == "confirmed"],
                 use_container_width=True, hide_index=True)
