"""Hackathon submission portal: minimum ground-staff headcount.

Run:  streamlit run app.py
Admin password: PORTAL_ADMIN_PASSWORD env var, or admin_password in .streamlit/secrets.toml
"""

from __future__ import annotations

import json
import os
from datetime import timedelta

import pandas as pd
import streamlit as st

import leaderboard
import store
import validator

st.set_page_config(page_title="Rostering Hackathon", page_icon="🛫", layout="wide")
store.init()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def admin_password() -> str | None:
    try:
        pw = st.secrets.get("admin_password")
    except Exception:
        pw = None
    return pw or os.environ.get("PORTAL_ADMIN_PASSWORD")


@st.cache_resource(show_spinner="Loading dataset…")
def load_ds(name: str, mtime: float):
    tasks, config = store.dataset_files(name)
    return validator.load_dataset(tasks, config)


def get_ds(name: str):
    tasks, config = store.dataset_files(name)
    mtime = max(os.path.getmtime(tasks), os.path.getmtime(config))
    return load_ds(name, mtime)


def weekly_limit() -> int:
    return int(store.get_setting("weekly_limit", "1"))


def submissions_open() -> bool:
    return store.get_setting("open", "1") == "1"


def week_reset_text() -> str:
    n = store.now()
    nxt = (n - timedelta(days=n.weekday())).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=7)
    return f"{nxt:%a %d %b %Y, %H:%M} ({store.TZ.key})"


def violations_df(rows: list[dict]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=["rule", "resource", "employee", "date", "message"])
    return pd.DataFrame(rows)[["rule", "resource", "employee", "date", "message"]]


def show_result(ds_name: str, r: dict, key: str) -> None:
    status = "✅ Valid" if r["valid"] else f"❌ Invalid — {r['n_violations']} broken rule(s)"
    with st.expander(f"**{ds_name}** · {status} · coverage {r['coverage'] * 100:.2f}% "
                     f"({r['tasks_covered']}/{r['tasks_total']}) · headcount {r['headcount']}",
                     expanded=not r["valid"]):
        by_res = json.loads(r["by_resource_json"]) if isinstance(r.get("by_resource_json"), str) else r.get("by_resource", {})
        if by_res:
            st.dataframe(pd.DataFrame([{"Resource": k, "Tasks covered": v["tasks_covered"],
                                        "Tasks total": v["tasks_total"], "Headcount": v["headcount"]}
                                       for k, v in sorted(by_res.items())]),
                         hide_index=True, width="stretch")
        for w in json.loads(r["warnings_json"]) if isinstance(r.get("warnings_json"), str) else []:
            st.warning(w)
        vs = json.loads(r["violations_json"]) if isinstance(r.get("violations_json"), str) else []
        if vs:
            df = violations_df(vs)
            st.write("Broken rules by type:")
            st.dataframe(df["rule"].value_counts().rename_axis("Rule").reset_index(name="Count"),
                         hide_index=True)
            st.dataframe(df.head(500), hide_index=True, width="stretch")
            if len(df) > 500:
                st.caption(f"Showing 500 of {len(df)}. Download the full list below.")
            st.download_button("Download broken rules (CSV)", df.to_csv(index=False).encode(),
                               file_name=f"{ds_name}_violations.csv", key=f"dl_{key}")


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #
def page_leaderboards():
    st.title("🏆 Leaderboards")
    datasets = store.list_datasets(active_only=True)
    if not datasets:
        st.info("No datasets published yet.")
        return
    results = store.all_results()
    names = [d["name"] for d in datasets]
    st.caption("Ranked by coverage (higher wins), then headcount (lower wins), then submission time "
               "(earlier wins). Each team's best valid result counts. The leaderboard doesn't decide the winners: "
               "finalists' code is tested on further datasets.")
    tabs = st.tabs(["Overall"] + names)
    with tabs[0]:
        st.caption("Average coverage over all datasets (a dataset with no valid result counts as 0%), "
                   "then total headcount over the datasets qualified on.")
        st.dataframe(leaderboard.overall(results, names), hide_index=True, width="stretch")
    for tab, d in zip(tabs[1:], datasets):
        with tab:
            lb = leaderboard.per_dataset(results, d["name"])
            c1, c2, c3 = st.columns(3)
            c1.metric("Teams on board", len(lb))
            c2.metric("Best coverage", f"{lb['Coverage %'].max():.2f}%" if len(lb) else "—")
            c3.metric("Time limit", f"{d['time_limit_s']} s" if d["time_limit_s"] else "—")
            st.dataframe(lb, hide_index=True, width="stretch")


def login_box() -> dict | None:
    team = st.session_state.get("team")
    if team:
        c1, c2 = st.columns([4, 1])
        c1.success(f"Signed in as **{team['name']}**")
        if c2.button("Sign out"):
            st.session_state.pop("team")
            st.rerun()
        return team
    with st.form("login"):
        teams = [t["name"] for t in store.list_teams()]
        name = st.selectbox("Team", teams, index=None, placeholder="Choose your team")
        code = st.text_input("Access code", type="password")
        if st.form_submit_button("Sign in"):
            t = store.check_team(name or "", code)
            if t:
                st.session_state["team"] = {"id": t["id"], "name": t["name"]}
                st.rerun()
            st.error("Team or access code is wrong.")
    return None


def page_submit():
    st.title("📤 Submit a roster")
    team = login_box()
    if not team:
        return
    datasets = store.list_datasets(active_only=True)
    if not datasets:
        st.info("No datasets published yet.")
        return

    if "last_results" in st.session_state:
        sid, rs = st.session_state.pop("last_results")
        st.success(f"Submission #{sid} recorded. Results:")
        for name, r in rs:
            show_result(name, r, key=f"new_{sid}_{name}")
        st.divider()

    wk = store.week_key(store.now())
    used = store.submissions_this_week(team["id"], wk)
    limit = weekly_limit()
    left = max(0, limit - used)
    st.info(f"Evaluated submissions this week: **{used} / {limit}**. The count resets {week_reset_text()}. "
            "Files that can't be read (missing columns, empty file) are rejected without using your submission.")

    with st.expander("Download dataset files"):
        for d in datasets:
            tasks, config = store.dataset_files(d["name"])
            c1, c2, c3 = st.columns([2, 2, 2])
            c1.write(f"**{d['name']}**" + (f" · time limit {d['time_limit_s']} s" if d["time_limit_s"] else ""))
            c2.download_button("tasks.json", open(tasks, "rb").read(), file_name=f"{d['name']}_tasks.json",
                               key=f"t_{d['id']}")
            c3.download_button("config.json", open(config, "rb").read(), file_name=f"{d['name']}_config.json",
                               key=f"c_{d['id']}")

    if not submissions_open():
        st.warning("Submissions are closed.")
        return
    if left == 0:
        st.warning("You have used this week's submission. You can submit again after the reset.")
        return

    st.write("Upload `shifts.csv` and `assignments.csv` for each dataset you want scored. "
             "Datasets you leave empty keep your earlier best result.")
    with st.form("submit", clear_on_submit=False):
        uploads = {}
        for d in datasets:
            st.subheader(d["name"])
            c1, c2 = st.columns(2)
            uploads[d["name"]] = (
                c1.file_uploader("shifts.csv", type=["csv"], key=f"s_{d['id']}"),
                c2.file_uploader("assignments.csv", type=["csv"], key=f"a_{d['id']}"),
            )
        note = st.text_input("Note (optional)", placeholder="e.g. CP-SAT + local search v3")
        sent = st.form_submit_button("Submit for evaluation", type="primary")

    if not sent:
        return

    # 1. pre-check: pairs complete and readable (does not use the weekly submission)
    chosen, errors = [], []
    for d in datasets:
        s, a = uploads[d["name"]]
        if not s and not a:
            continue
        if not (s and a):
            errors.append(f"{d['name']}: upload both shifts.csv and assignments.csv.")
            continue
        sb, ab = s.getvalue(), a.getvalue()
        try:
            validator.check_format(sb, ab)
        except (validator.FormatError, UnicodeDecodeError) as e:
            errors.append(f"{d['name']}: {e}")
            continue
        chosen.append((d, sb, ab))
    if errors:
        for e in errors:
            st.error(e)
        st.info("Nothing was recorded; your weekly submission is unused.")
        return
    if not chosen:
        st.error("Upload files for at least one dataset.")
        return
    # re-check the limit at submit time (two tabs open, etc.)
    if store.submissions_this_week(team["id"], wk) >= limit:
        st.warning("This week's submission has already been used.")
        return

    # 2. validate and record
    results = []
    with st.spinner("Validating…"):
        for d, sb, ab in chosen:
            res = validator.validate(get_ds(d["name"]), sb, ab)
            results.append((d, res, {"shifts.csv": sb, "assignments.csv": ab}))
    sid = store.record_submission(team["id"], note, results)
    st.session_state["last_results"] = (sid, [(d["name"], {
        "valid": res.valid, "n_violations": len(res.violations), "coverage": res.coverage,
        "tasks_covered": res.tasks_covered, "tasks_total": res.tasks_total, "headcount": res.headcount,
        "by_resource_json": json.dumps(res.by_resource), "warnings_json": json.dumps(res.warnings),
        "violations_json": json.dumps([v.as_dict() for v in res.violations[:5000]]),
    }) for d, res, _ in results])
    st.rerun()


def page_my_submissions():
    st.title("🗂️ My submissions")
    team = login_box()
    if not team:
        return
    rows = store.team_results(team["id"])
    if not rows:
        st.info("No submissions yet.")
        return
    by_sub: dict[int, list[dict]] = {}
    for r in rows:
        by_sub.setdefault(r["submission_id"], []).append(r)
    for sid, rs in by_sub.items():
        st.subheader(f"Submission #{sid} · {rs[0]['submitted_at'][:16].replace('T', ' ')}"
                     + (f" · {rs[0]['note']}" if rs[0]["note"] else ""))
        for r in rs:
            show_result(r["dataset"], r, key=f"mine_{r['id']}")


def page_admin():
    st.title("🔧 Admin")
    pw = admin_password()
    if not pw:
        st.error("Set PORTAL_ADMIN_PASSWORD (or admin_password in .streamlit/secrets.toml) to enable admin.")
        return
    if not st.session_state.get("admin"):
        with st.form("admin_login"):
            p = st.text_input("Admin password", type="password")
            if st.form_submit_button("Sign in"):
                if p == pw:
                    st.session_state["admin"] = True
                    st.rerun()
                st.error("Wrong password.")
        return

    t_teams, t_ds, t_subs, t_set = st.tabs(["Teams", "Datasets", "Submissions", "Settings"])

    with t_teams:
        with st.form("add_team", clear_on_submit=True):
            name = st.text_input("New team name")
            if st.form_submit_button("Add team") and name.strip():
                try:
                    code = store.add_team(name)
                    st.session_state["new_code"] = (name.strip(), code)
                except Exception as e:  # unique name
                    st.error(f"Could not add team: {e}")
        if "new_code" in st.session_state:
            n, c = st.session_state["new_code"]
            st.success(f"Access code for **{n}** (shown once, send it to the team):")
            st.code(c)
        for t in store.list_teams():
            c1, c2, c3 = st.columns([3, 1, 1])
            c1.write(f"**{t['name']}** · added {t['created_at'][:10]}")
            if c2.button("New code", key=f"rc_{t['id']}"):
                st.session_state["new_code"] = (t["name"], store.reset_code(t["id"]))
                st.rerun()
            if c3.button("Delete", key=f"dt_{t['id']}"):
                store.delete_team(t["id"])
                st.session_state.pop("new_code", None)
                st.rerun()

    with t_ds:
        with st.form("add_ds", clear_on_submit=True):
            st.write("Add or replace a dataset (same name replaces its files; rescore afterwards).")
            name = st.text_input("Dataset name", placeholder="D1")
            c1, c2 = st.columns(2)
            tf = c1.file_uploader("tasks_json", type=["json"])
            cf = c2.file_uploader("wfo_run_configuration_json", type=["json"])
            c3, c4 = st.columns(2)
            tl = c3.number_input("Time limit (seconds)", min_value=0, value=600, step=60)
            order = c4.number_input("Display order", min_value=0, value=len(store.list_datasets()), step=1)
            if st.form_submit_button("Save dataset"):
                if not (name.strip() and tf and cf):
                    st.error("Give a name and both files.")
                else:
                    try:
                        ds = validator.load_dataset(tf.getvalue(), cf.getvalue())
                        store.add_dataset(name.strip(), tf.getvalue(), cf.getvalue(), int(tl) or None, int(order))
                        st.success(f"Saved {name}: {len(ds.tasks)} tasks in the horizon, "
                                   f"{len(ds.rules)} resource(s), {ds.n_weeks} week(s), {ds.schedule_type}.")
                    except Exception as e:
                        st.error(f"Could not read the dataset: {e}")
        for d in store.list_datasets():
            c1, c2 = st.columns([4, 1])
            try:
                info = get_ds(d["name"])
                desc = (f"{len(info.tasks)} tasks · {', '.join(sorted(info.rules))} · {info.n_weeks} wk · "
                        f"{info.schedule_type}")
            except Exception as e:
                desc = f"error: {e}"
            c1.write(f"**{d['name']}** · {desc} · limit {d['time_limit_s'] or '—'} s · "
                     f"{'live' if d['active'] else 'hidden'}")
            if c2.button("Hide" if d["active"] else "Publish", key=f"act_{d['id']}"):
                store.set_dataset_active(d["id"], not d["active"])
                st.rerun()

    with t_subs:
        subs = store.list_submissions()
        if not subs:
            st.info("No submissions yet.")
        else:
            df = pd.DataFrame(subs)[["id", "team", "submitted_at", "week_key", "n_datasets", "n_valid", "note"]]
            st.dataframe(df, hide_index=True, width="stretch")
            c1, c2 = st.columns([2, 1])
            sid = c1.selectbox("Submission", [s["id"] for s in subs],
                               format_func=lambda i: f"#{i} · " + next(s["team"] for s in subs if s["id"] == i))
            if c2.button("Delete submission (frees the team's weekly slot)"):
                store.delete_submission(sid)
                st.rerun()
            for r in [r for r in store.all_results() if r["submission_id"] == sid]:
                show_result(r["dataset"], r, key=f"adm_{r['id']}")
        st.divider()
        if st.button("Rescore every submission with the current validator and dataset files"):
            n = 0
            with st.spinner("Rescoring…"):
                for r in store.all_results():
                    files = store.submission_files(r["team_id"], r["submission_id"], r["dataset"])
                    if all(os.path.exists(p) for p in files.values()):
                        res = validator.validate(get_ds(r["dataset"]), files["shifts.csv"], files["assignments.csv"])
                        store.update_result(r["id"], res)
                        n += 1
            st.success(f"Rescored {n} result(s).")

    with t_set:
        with st.form("settings"):
            lim = st.number_input("Evaluated submissions per team per week", min_value=1, value=weekly_limit())
            op = st.toggle("Submissions open", value=submissions_open())
            if st.form_submit_button("Save"):
                store.set_setting("weekly_limit", str(int(lim)))
                store.set_setting("open", "1" if op else "0")
                st.success("Saved.")
        st.caption(f"Weeks run Monday 00:00 to Sunday 23:59 in {store.TZ.key} (set PORTAL_TZ to change).")
        if st.button("Sign out of admin"):
            st.session_state.pop("admin")
            st.rerun()


PAGES = {
    "Leaderboards": page_leaderboards,
    "Submit": page_submit,
    "My submissions": page_my_submissions,
    "Admin": page_admin,
}

with st.sidebar:
    st.header("Rostering Hackathon")
    choice = st.radio("Go to", list(PAGES), label_visibility="collapsed")
    st.caption("Minimum ground-staff headcount")

PAGES[choice]()
