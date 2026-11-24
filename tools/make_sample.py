"""Generate a small synthetic dataset plus a simple valid roster for testing the portal.

    python tools/make_sample.py [out_dir] [--cyclic] [--seed N]

Writes tasks.json, config.json, shifts.csv and assignments.csv into out_dir.
The roster is a naive baseline (fixed daily start times, greedy task assignment),
so it is valid but far from optimal.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
from datetime import date, datetime, timedelta

HORIZON_START = date(2026, 11, 9)  # a Monday
WEEKS = 2


def make_dataset(seed: int, cyclic: bool) -> tuple[dict, dict]:
    rnd = random.Random(seed)
    task_defs = []
    tid = 1
    for res, n_defs in (("LDR", 6), ("CLN", 4)):
        for _ in range(n_defs):
            start = rnd.randrange(6 * 60 + 30, 20 * 60, 5)
            dur = rnd.choice([30, 45, 60, 90])
            end = start + dur
            task_defs.append({
                "Id": tid, "Resource": res, "Service": "RMP" if res == "LDR" else "CLN",
                "DemandDateDelta": 0,
                "Start": f"{start // 60:02d}:{start % 60:02d}", "End": f"{end // 60:02d}:{end % 60:02d}",
                "DurationMinutes": dur, "NumResources": rnd.choice([1, 2, 4, 8] if res == "LDR" else [1, 2]),
            })
            tid += 1
    # one task that fits no legal shift: 10 hours long
    task_defs.append({"Id": tid, "Resource": "LDR", "Service": "RMP", "DemandDateDelta": 0,
                      "Start": "08:00", "End": "18:00", "DurationMinutes": 600, "NumResources": 1})

    turns = []
    utid = 1
    for d in range(7 * WEEKS):
        day = HORIZON_START + timedelta(days=d)
        for _ in range(rnd.randint(3, 6)):
            ids = rnd.sample([t["Id"] for t in task_defs[:-1]], k=3)
            if rnd.random() < 0.1:
                ids.append(task_defs[-1]["Id"])
            turns.append({
                "UniqueTurnID": utid, "TurnID": f"T{utid:05d}", "DemandDate": day.isoformat(),
                "DemandDay": day.strftime("%a").upper(), "ACType": "NARROW", "Tasks": ids,
            })
            utid += 1

    hs = f"{HORIZON_START.isoformat()}T00:00:00"
    he = f"{(HORIZON_START + timedelta(days=7 * WEEKS - 1)).isoformat()}T23:59:59"
    tasks = {"PlanningHorizon": {"PlanningHorizonStart": hs, "PlanningHorizonEnd": he},
             "Turnarounds": turns, "Tasks": task_defs}

    def res_cfg(name, lengths, half, hours, dmin, dmax, rest, consec, cal):
        return {"resource": name, "resource_tag": "DYNAMIC", "shift_configuration": {
            "shift_restrictions": {
                "shift_start_restrictions": [{"start_time": "23:01", "end_time": "05:59"}],
                "shift_end_restrictions": [{"start_time": "23:01", "end_time": "02:59"}]},
            "shift_policy": {"daily": {"include_30_min_increments": half, "shift_lengths": lengths},
                             "weekly": {"max_working_hours": hours, "min_days_off": dmin, "max_days_off": dmax,
                                        "minimum_rest_between_consecutive_shifts": rest}},
            "consecutive_day_off": consec, "enable_calendar_day_off": cal}}

    config = {"PlanningHorizonStart": hs, "PlanningHorizonEnd": he,
              "schedule_type": "CYCLIC" if cyclic else "IRREGULAR",
              "resources": [res_cfg("LDR", [8], False, 48, 1, 1, 12, False, False),
                            res_cfg("CLN", [8, 9], True, 40, 2, 2, 12, True, True)]}
    return tasks, config


def naive_roster(tasks: dict, config: dict) -> tuple[list[list], list[list]]:
    """Fixed-start baseline: each employee starts at the same time every working day."""
    import validator
    ds = validator.load_dataset(tasks, config)
    shifts, assigns = [], []
    plan = {"LDR": (6, 8 * 60, [6 * 60, 14 * 60]), "CLN": (5, 8 * 60, [6 * 60, 14 * 60])}
    emp_no = 0
    for res, (work_days, length, starts) in plan.items():
        res_tasks = sorted((t for t in ds.tasks.values() if t.resource == res), key=lambda t: t.start)
        peak = max((t.need for t in res_tasks), default=1)
        emps = []  # (emp_id, start_min, off_days)
        for st in starts:
            for k in range(peak + 3):
                emp_no += 1
                off_first = k % (work_days + 1)
                off = {(off_first + j) % 7 for j in range(7 - work_days)}
                emps.append((f"E{emp_no:03d}", st, off))
        emp_shifts = {}
        for emp, st, off in emps:
            for d in range(7 * WEEKS):
                day = HORIZON_START + timedelta(days=d)
                if d % 7 in off:
                    continue
                start = datetime.combine(day, datetime.min.time()) + timedelta(minutes=st)
                shifts.append([res, emp, day.isoformat(), f"{st // 60:02d}:{st % 60:02d}", length / 60])
                emp_shifts.setdefault(emp, []).append((start, start + timedelta(minutes=length)))
        busy = {emp: [] for emp, _, _ in emps}
        for t in res_tasks:
            got = 0
            for emp, _, _ in emps:
                if got >= t.need:
                    break
                if not any(a <= t.start and b >= t.end for a, b in emp_shifts.get(emp, [])):
                    continue
                if any(not (t.end <= s or t.start >= e) for s, e in busy[emp]):
                    continue
                busy[emp].append((t.start, t.end))
                assigns.append([res, emp, t.key[0], t.key[1]])
                got += 1
    return shifts, assigns


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out", nargs="?", default="sample_data/dataset_A")
    ap.add_argument("--cyclic", action="store_true")
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    tasks, config = make_dataset(a.seed, a.cyclic)
    with open(os.path.join(a.out, "tasks.json"), "w") as f:
        json.dump(tasks, f, indent=2)
    with open(os.path.join(a.out, "config.json"), "w") as f:
        json.dump(config, f, indent=2)
    shifts, assigns = naive_roster(tasks, config)
    with open(os.path.join(a.out, "shifts.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["resource", "employee_id", "date", "shift_start", "shift_length_hours"])
        w.writerows([[r, e, d, s, f"{h:g}"] for r, e, d, s, h in shifts])
    with open(os.path.join(a.out, "assignments.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["resource", "employee_id", "unique_turn_id", "task_id"])
        w.writerows(assigns)
    print(f"wrote {a.out}: {len(shifts)} shifts, {len(assigns)} assignments")


if __name__ == "__main__":
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    main()
