# Rostering Hackathon Portal

Streamlit app where teams upload rosters, get them validated against each dataset, and see per-dataset and overall leaderboards.

## Run

```bash
pip install -r requirements.txt
export PORTAL_ADMIN_PASSWORD='choose-one'   # or .streamlit/secrets.toml (see the .example)
streamlit run app.py
```

Optional env vars: `PORTAL_DATA_DIR` (default `./data`), `PORTAL_TZ` (default `Asia/Kolkata`; weeks run Mon 00:00 to Sun 23:59 in this zone).

## Organiser setup (Admin page)

1. **Datasets**: give a name (e.g. D1), upload `tasks_json` and `wfo_run_configuration_json`, set the time limit. Datasets can be hidden or published; saving the same name replaces its files.
2. **Teams**: add each team and send it the access code (shown once; "New code" resets it).
3. **Settings**: submissions per team per week (default 1) and an open/closed switch.

## Participant flow

Sign in with team + code, then upload `shifts.csv` and `assignments.csv` for any datasets. Unreadable files (missing columns, empty, half a pair) are rejected **without** using the weekly slot. Everything else is validated, recorded and shown with each broken rule (downloadable as CSV). "My submissions" keeps the history.

## Scoring

- Valid = no broken rule. Coverage = tasks fully staffed / tasks starting inside the horizon. Headcount = distinct employees.
- Per dataset: each team's best valid result; coverage ↓, headcount ↑, then earlier submission wins (run time isn't measured because only output files are uploaded).
- Overall: average coverage over all published datasets (none valid = 0%), then total headcount over datasets qualified on.

## Rules checked (`validator.py`)

Shift length (incl. half-hour steps), start on :00/:30, forbidden start/end windows (inclusive, cross midnight), one shift per day, exact weekly hours per Mon–Sun week, days-off range, consecutive days off, minimum rest (across weeks; wraps around for CYCLIC), calendar day off, identical weekly pattern for CYCLIC, task inside one of the employee's shifts, no overlapping tasks per person, resource match, unknown/out-of-horizon tasks, duplicate rows.

Same validator from the command line (you can share this file with participants):

```bash
python validator.py tasks.json config.json shifts.csv assignments.csv
```

Admin → Submissions has **Rescore** to re-run all stored submissions after a validator or dataset change, and **Delete** to free a team's weekly slot.

## Tests and sample data

```bash
python tools/make_sample.py sample_data/dataset_A            # synthetic dataset + naive valid roster
python tools/make_sample.py sample_data/dataset_B --cyclic --seed 11
python -m pytest -q tests
```

## Notes

- Built for a single server process (SQLite). For many concurrent users, put it behind one instance or move to Postgres.
- The quality bar against the best-known solution isn't implemented yet.
