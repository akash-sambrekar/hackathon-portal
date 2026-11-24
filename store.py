"""SQLite storage for teams, datasets, submissions and results."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

DATA_DIR = os.environ.get("PORTAL_DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
DB_PATH = os.path.join(DATA_DIR, "portal.db")
TZ = ZoneInfo(os.environ.get("PORTAL_TZ", "Asia/Kolkata"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS teams (
    id INTEGER PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    code_salt TEXT NOT NULL,
    code_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS datasets (
    id INTEGER PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    time_limit_s INTEGER,
    active INTEGER NOT NULL DEFAULT 1,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS submissions (
    id INTEGER PRIMARY KEY,
    team_id INTEGER NOT NULL REFERENCES teams(id),
    submitted_at TEXT NOT NULL,
    week_key TEXT NOT NULL,
    note TEXT
);
CREATE TABLE IF NOT EXISTS results (
    id INTEGER PRIMARY KEY,
    submission_id INTEGER NOT NULL REFERENCES submissions(id) ON DELETE CASCADE,
    dataset_id INTEGER NOT NULL REFERENCES datasets(id),
    valid INTEGER NOT NULL,
    coverage REAL NOT NULL,
    tasks_covered INTEGER NOT NULL,
    tasks_total INTEGER NOT NULL,
    headcount INTEGER NOT NULL,
    n_violations INTEGER NOT NULL,
    violations_json TEXT NOT NULL,
    warnings_json TEXT NOT NULL,
    by_resource_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def now() -> datetime:
    return datetime.now(TZ)


def week_key(dt: datetime) -> str:
    y, w, _ = dt.isocalendar()
    return f"{y}-W{w:02d}"


@contextmanager
def conn():
    os.makedirs(DATA_DIR, exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init() -> None:
    with conn() as c:
        c.executescript(SCHEMA)


# ---- settings -------------------------------------------------------------- #
def get_setting(key: str, default: str) -> str:
    with conn() as c:
        r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def set_setting(key: str, value: str) -> None:
    with conn() as c:
        c.execute("INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (key, value))


# ---- teams ----------------------------------------------------------------- #
def _hash(code: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", code.encode(), salt.encode(), 100_000).hex()


def add_team(name: str) -> str:
    """Create a team and return its access code (shown once)."""
    code = secrets.token_urlsafe(9)
    salt = secrets.token_hex(8)
    with conn() as c:
        c.execute("INSERT INTO teams(name, code_salt, code_hash, created_at) VALUES(?,?,?,?)",
                  (name.strip(), salt, _hash(code, salt), now().isoformat()))
    return code


def reset_code(team_id: int) -> str:
    code = secrets.token_urlsafe(9)
    salt = secrets.token_hex(8)
    with conn() as c:
        c.execute("UPDATE teams SET code_salt=?, code_hash=? WHERE id=?", (salt, _hash(code, salt), team_id))
    return code


def check_team(name: str, code: str):
    with conn() as c:
        r = c.execute("SELECT * FROM teams WHERE name=?", (name.strip(),)).fetchone()
    if r and secrets.compare_digest(_hash(code.strip(), r["code_salt"]), r["code_hash"]):
        return dict(r)
    return None


def list_teams() -> list[dict]:
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT id, name, created_at FROM teams ORDER BY name")]


def delete_team(team_id: int) -> None:
    with conn() as c:
        c.execute("DELETE FROM results WHERE submission_id IN (SELECT id FROM submissions WHERE team_id=?)", (team_id,))
        c.execute("DELETE FROM submissions WHERE team_id=?", (team_id,))
        c.execute("DELETE FROM teams WHERE id=?", (team_id,))
    shutil.rmtree(os.path.join(DATA_DIR, "submissions", f"team_{team_id}"), ignore_errors=True)


# ---- datasets -------------------------------------------------------------- #
def dataset_dir(name: str) -> str:
    return os.path.join(DATA_DIR, "datasets", name)


def add_dataset(name: str, tasks_bytes: bytes, config_bytes: bytes, time_limit_s: int | None, sort_order: int = 0):
    d = dataset_dir(name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "tasks.json"), "wb") as f:
        f.write(tasks_bytes)
    with open(os.path.join(d, "config.json"), "wb") as f:
        f.write(config_bytes)
    with conn() as c:
        c.execute("""INSERT INTO datasets(name, time_limit_s, sort_order, created_at) VALUES(?,?,?,?)
                     ON CONFLICT(name) DO UPDATE SET time_limit_s=excluded.time_limit_s,
                     sort_order=excluded.sort_order""",
                  (name, time_limit_s, sort_order, now().isoformat()))


def list_datasets(active_only: bool = False) -> list[dict]:
    q = "SELECT * FROM datasets" + (" WHERE active=1" if active_only else "") + " ORDER BY sort_order, name"
    with conn() as c:
        return [dict(r) for r in c.execute(q)]


def set_dataset_active(ds_id: int, active: bool) -> None:
    with conn() as c:
        c.execute("UPDATE datasets SET active=? WHERE id=?", (1 if active else 0, ds_id))


def dataset_files(name: str) -> tuple[str, str]:
    d = dataset_dir(name)
    return os.path.join(d, "tasks.json"), os.path.join(d, "config.json")


# ---- submissions ----------------------------------------------------------- #
def submissions_this_week(team_id: int, wk: str) -> int:
    with conn() as c:
        return c.execute("SELECT COUNT(*) FROM submissions WHERE team_id=? AND week_key=?",
                         (team_id, wk)).fetchone()[0]


def record_submission(team_id: int, note: str, results: list[tuple[dict, object, dict[str, bytes]]]) -> int:
    """results: list of (dataset_row, validator.Result, {"shifts.csv": bytes, "assignments.csv": bytes})."""
    ts = now()
    with conn() as c:
        cur = c.execute("INSERT INTO submissions(team_id, submitted_at, week_key, note) VALUES(?,?,?,?)",
                        (team_id, ts.isoformat(), week_key(ts), note))
        sid = cur.lastrowid
        for ds, res, files in results:
            c.execute("""INSERT INTO results(submission_id, dataset_id, valid, coverage, tasks_covered, tasks_total,
                         headcount, n_violations, violations_json, warnings_json, by_resource_json)
                         VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                      (sid, ds["id"], int(res.valid), res.coverage, res.tasks_covered, res.tasks_total,
                       res.headcount, len(res.violations),
                       json.dumps([v.as_dict() for v in res.violations[:5000]]),
                       json.dumps(res.warnings), json.dumps(res.by_resource)))
            d = os.path.join(DATA_DIR, "submissions", f"team_{team_id}", f"sub_{sid}", ds["name"])
            os.makedirs(d, exist_ok=True)
            for fname, data in files.items():
                with open(os.path.join(d, fname), "wb") as f:
                    f.write(data)
    return sid


def delete_submission(sid: int) -> None:
    with conn() as c:
        r = c.execute("SELECT team_id FROM submissions WHERE id=?", (sid,)).fetchone()
        c.execute("DELETE FROM results WHERE submission_id=?", (sid,))
        c.execute("DELETE FROM submissions WHERE id=?", (sid,))
    if r:
        shutil.rmtree(os.path.join(DATA_DIR, "submissions", f"team_{r['team_id']}", f"sub_{sid}"), ignore_errors=True)


def all_results() -> list[dict]:
    with conn() as c:
        return [dict(r) for r in c.execute("""
            SELECT r.*, s.team_id, s.submitted_at, s.week_key, s.note, t.name AS team, d.name AS dataset
            FROM results r JOIN submissions s ON s.id = r.submission_id
            JOIN teams t ON t.id = s.team_id JOIN datasets d ON d.id = r.dataset_id
            ORDER BY s.submitted_at DESC""")]


def team_results(team_id: int) -> list[dict]:
    return [r for r in all_results() if r["team_id"] == team_id]


def list_submissions() -> list[dict]:
    with conn() as c:
        return [dict(r) for r in c.execute("""
            SELECT s.*, t.name AS team, COUNT(r.id) AS n_datasets, SUM(r.valid) AS n_valid
            FROM submissions s JOIN teams t ON t.id = s.team_id LEFT JOIN results r ON r.submission_id = s.id
            GROUP BY s.id ORDER BY s.submitted_at DESC""")]


def submission_files(team_id: int, sid: int, dataset: str) -> dict[str, str]:
    d = os.path.join(DATA_DIR, "submissions", f"team_{team_id}", f"sub_{sid}", dataset)
    return {f: os.path.join(d, f) for f in ("shifts.csv", "assignments.csv")}


def update_result(result_id: int, res) -> None:
    with conn() as c:
        c.execute("""UPDATE results SET valid=?, coverage=?, tasks_covered=?, tasks_total=?, headcount=?,
                     n_violations=?, violations_json=?, warnings_json=?, by_resource_json=? WHERE id=?""",
                  (int(res.valid), res.coverage, res.tasks_covered, res.tasks_total, res.headcount,
                   len(res.violations), json.dumps([v.as_dict() for v in res.violations[:5000]]),
                   json.dumps(res.warnings), json.dumps(res.by_resource), result_id))
