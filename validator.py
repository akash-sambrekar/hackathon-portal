"""Roster validator for the minimum ground-staff headcount hackathon.

Checks a submitted roster (shifts.csv + assignments.csv) against a dataset
(tasks json + wfo run configuration json) and reports:

* every broken rule, with the rule number from the case study, employee and date;
* coverage = tasks fully staffed / tasks starting inside the planning horizon;
* headcount = number of distinct employees on the roster.

A roster is valid only if it breaks no rule. Can be used as a library
(`validate(...)`) or from the command line:

    python validator.py tasks.json config.json shifts.csv assignments.csv
"""

from __future__ import annotations

import csv
import io
import json
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Iterable

SHIFT_COLUMNS = ["resource", "employee_id", "date", "shift_start", "shift_length_hours"]
ASSIGN_COLUMNS = ["resource", "employee_id", "unique_turn_id", "task_id"]



class FormatError(Exception):
    """Raised when an input file cannot be read at all."""


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #
@dataclass
class Task:
    key: tuple[int, int]  # (UniqueTurnID, Id)
    resource: str
    start: datetime
    end: datetime
    need: int


@dataclass
class ResourceRules:
    resource: str
    lengths_min: set[int]
    start_windows: list[tuple[int, int]]
    end_windows: list[tuple[int, int]]
    weekly_minutes: int
    min_days_off: int
    max_days_off: int
    rest_minutes: int
    consecutive_day_off: bool
    calendar_day_off: bool


@dataclass
class Dataset:
    horizon_start: datetime
    horizon_end: datetime
    schedule_type: str
    rules: dict[str, ResourceRules]
    tasks: dict[tuple[int, int], Task]

    @property
    def n_weeks(self) -> int:
        days = (self.horizon_end.date() - self.horizon_start.date()).days + 1
        return max(1, days // 7)

    @property
    def first_day(self) -> date:
        return self.horizon_start.date()


@dataclass
class Shift:
    resource: str
    employee: str
    day: date
    start: datetime
    end: datetime
    length_min: int
    row: int


@dataclass
class Violation:
    rule: str
    message: str
    resource: str = ""
    employee: str = ""
    date: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "resource": self.resource,
            "employee": self.employee,
            "date": self.date,
            "message": self.message,
        }


@dataclass
class Result:
    valid: bool
    coverage: float
    tasks_covered: int
    tasks_total: int
    headcount: int
    violations: list[Violation] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    by_resource: dict[str, dict[str, int]] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "coverage": self.coverage,
            "tasks_covered": self.tasks_covered,
            "tasks_total": self.tasks_total,
            "headcount": self.headcount,
            "n_violations": len(self.violations),
        }


# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #
def _hhmm_to_min(s: str) -> int:
    s = str(s).strip()
    parts = s.split(":")
    if len(parts) < 2:
        raise ValueError(f"bad time '{s}'")
    h, m = int(parts[0]), int(parts[1])
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"bad time '{s}'")
    return h * 60 + m


def _in_window(t: int, window: tuple[int, int]) -> bool:
    """Inclusive minute-of-day window that may cross midnight."""
    s, e = window
    if s <= e:
        return s <= t <= e
    return t >= s or t <= e


def _parse_dt(s: str) -> datetime:
    return datetime.fromisoformat(str(s).strip().replace("Z", ""))


def _load_json(obj: Any) -> dict:
    if isinstance(obj, dict):
        return obj
    if isinstance(obj, (bytes, bytearray)):
        obj = obj.decode("utf-8-sig")
    if isinstance(obj, str):
        if "\n" not in obj and os.path.isfile(obj):
            with open(obj, encoding="utf-8-sig") as f:
                return json.load(f)
        return json.loads(obj)
    return json.load(obj)


def _allowed_lengths_min(lengths: Iterable[float], half_hours: bool) -> set[int]:
    base = sorted({int(round(float(x) * 60)) for x in lengths})
    allowed = set(base)
    if half_hours:
        for a, b in zip(base, base[1:]):
            t = a + 30
            while t < b:
                allowed.add(t)
                t += 30
    return allowed


def load_dataset(tasks_json: Any, config_json: Any) -> Dataset:
    """Build a Dataset from the two organiser input files (path, text, bytes or dict)."""
    tj = _load_json(tasks_json)
    cj = _load_json(config_json)

    hs = _parse_dt(cj.get("PlanningHorizonStart") or tj["PlanningHorizon"]["PlanningHorizonStart"])
    he = _parse_dt(cj.get("PlanningHorizonEnd") or tj["PlanningHorizon"]["PlanningHorizonEnd"])

    rules: dict[str, ResourceRules] = {}
    for r in cj.get("resources", []):
        sc = r["shift_configuration"]
        restr = sc.get("shift_restrictions", {}) or {}
        daily = sc["shift_policy"]["daily"]
        weekly = sc["shift_policy"]["weekly"]
        rules[r["resource"]] = ResourceRules(
            resource=r["resource"],
            lengths_min=_allowed_lengths_min(daily["shift_lengths"], bool(daily.get("include_30_min_increments"))),
            start_windows=[(_hhmm_to_min(w["start_time"]), _hhmm_to_min(w["end_time"]))
                           for w in restr.get("shift_start_restrictions", []) or []],
            end_windows=[(_hhmm_to_min(w["start_time"]), _hhmm_to_min(w["end_time"]))
                         for w in restr.get("shift_end_restrictions", []) or []],
            weekly_minutes=int(round(float(weekly["max_working_hours"]) * 60)),
            min_days_off=int(weekly.get("min_days_off", 0)),
            max_days_off=int(weekly.get("max_days_off", 7)),
            rest_minutes=int(round(float(weekly.get("minimum_rest_between_consecutive_shifts", 0)) * 60)),
            consecutive_day_off=bool(sc.get("consecutive_day_off", False)),
            calendar_day_off=bool(sc.get("enable_calendar_day_off", False)),
        )

    defs = {int(t["Id"]): t for t in tj.get("Tasks", [])}
    tasks: dict[tuple[int, int], Task] = {}
    for turn in tj.get("Turnarounds", []):
        utid = int(turn["UniqueTurnID"])
        d0 = date.fromisoformat(str(turn["DemandDate"])[:10])
        for tid in turn.get("Tasks", []):
            td = defs.get(int(tid))
            if td is None:
                continue
            d = d0 + timedelta(days=int(td.get("DemandDateDelta", 0) or 0))
            s_min, e_min = _hhmm_to_min(td["Start"]), _hhmm_to_min(td["End"])
            start = datetime.combine(d, datetime.min.time()) + timedelta(minutes=s_min)
            end = datetime.combine(d, datetime.min.time()) + timedelta(minutes=e_min)
            if e_min <= s_min:
                end += timedelta(days=1)
            if not (hs <= start <= he):
                continue  # rule 13: only tasks starting inside the horizon count
            key = (utid, int(tid))
            tasks[key] = Task(key, str(td["Resource"]), start, end, int(td.get("NumResources", 1)))

    return Dataset(hs, he, str(cj.get("schedule_type", "IRREGULAR")).upper(), rules, tasks)


def _read_csv(data: Any, required: list[str], name: str) -> list[dict[str, str]]:
    if isinstance(data, (bytes, bytearray)):
        data = data.decode("utf-8-sig")
    elif isinstance(data, str) and "\n" not in data and os.path.isfile(data):
        with open(data, encoding="utf-8-sig") as f:
            data = f.read()
    if not isinstance(data, str):
        data = data.read()
        if isinstance(data, bytes):
            data = data.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(data))
    if reader.fieldnames is None:
        raise FormatError(f"{name} is empty")
    cols = [c.strip().lower() for c in reader.fieldnames]
    missing = [c for c in required if c not in cols]
    if missing:
        raise FormatError(f"{name} is missing column(s): {', '.join(missing)}")
    rows = []
    for raw in reader:
        rows.append({k.strip().lower(): (v or "").strip() for k, v in raw.items() if k is not None})
    return rows


def check_format(shifts_csv: Any, assignments_csv: Any) -> None:
    """Raise FormatError if either file can't be read as the expected CSV."""
    _read_csv(shifts_csv, SHIFT_COLUMNS, "shifts.csv")
    _read_csv(assignments_csv, ASSIGN_COLUMNS, "assignments.csv")


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def validate(ds: Dataset, shifts_csv: Any, assignments_csv: Any) -> Result:
    v: list[Violation] = []
    warnings: list[str] = []

    shift_rows = _read_csv(shifts_csv, SHIFT_COLUMNS, "shifts.csv")
    assign_rows = _read_csv(assignments_csv, ASSIGN_COLUMNS, "assignments.csv")

    first_day = ds.first_day
    last_day = ds.horizon_end.date()
    n_weeks = ds.n_weeks

    # ---- parse shifts ---------------------------------------------------- #
    emp_resource: dict[str, str] = {}
    shifts_by_emp: dict[str, list[Shift]] = defaultdict(list)
    for i, r in enumerate(shift_rows, start=2):
        res, emp = r["resource"], r["employee_id"]
        where = dict(resource=res, employee=emp, date=r["date"])
        if not emp:
            v.append(Violation("File format", f"shifts.csv line {i}: empty employee_id", **where))
            continue
        if res not in ds.rules:
            v.append(Violation("File format", f"shifts.csv line {i}: unknown resource '{res}'", **where))
            continue
        if emp in emp_resource and emp_resource[emp] != res:
            v.append(Violation("File format", f"shifts.csv line {i}: employee works for two resources "
                                  f"({emp_resource[emp]} and {res})", **where))
            continue
        emp_resource[emp] = res
        try:
            d = date.fromisoformat(r["date"][:10])
            s_min = _hhmm_to_min(r["shift_start"])
            length_min = int(round(float(r["shift_length_hours"]) * 60))
        except (ValueError, TypeError) as e:
            v.append(Violation("File format", f"shifts.csv line {i}: {e}", **where))
            continue
        if not (first_day <= d <= last_day):
            v.append(Violation("File format", f"shifts.csv line {i}: date {d} is outside the planning horizon", **where))
            continue
        start = datetime.combine(d, datetime.min.time()) + timedelta(minutes=s_min)
        shifts_by_emp[emp].append(Shift(res, emp, d, start, start + timedelta(minutes=length_min), length_min, i))

    # ---- per-employee shift and weekly rules ----------------------------- #
    for emp, shifts in shifts_by_emp.items():
        res = emp_resource[emp]
        rules = ds.rules[res]
        shifts.sort(key=lambda s: s.start)

        days_seen: dict[date, Shift] = {}
        for s in shifts:
            where = dict(resource=res, employee=emp, date=s.day.isoformat())
            if s.length_min not in rules.lengths_min:
                allowed = ", ".join(f"{m / 60:g}" for m in sorted(rules.lengths_min))
                v.append(Violation("Shift length", f"shift length {s.length_min / 60:g} h not allowed (allowed: {allowed})", **where))
            st_min = s.start.hour * 60 + s.start.minute
            if st_min % 30 != 0:
                v.append(Violation("Shift start time", f"shift starts at {s.start:%H:%M}, not on the hour or half hour", **where))
            for w in rules.start_windows:
                if _in_window(st_min, w):
                    v.append(Violation("Forbidden start/end window", f"shift starts at {s.start:%H:%M}, inside forbidden start window "
                                          f"{w[0] // 60:02d}:{w[0] % 60:02d}–{w[1] // 60:02d}:{w[1] % 60:02d}", **where))
            en_min = s.end.hour * 60 + s.end.minute
            for w in rules.end_windows:
                if _in_window(en_min, w):
                    v.append(Violation("Forbidden start/end window", f"shift ends at {s.end:%H:%M}, inside forbidden end window "
                                          f"{w[0] // 60:02d}:{w[0] % 60:02d}–{w[1] // 60:02d}:{w[1] % 60:02d}", **where))
            if s.day in days_seen:
                v.append(Violation("One shift per day", "more than one shift on this day", **where))
            else:
                days_seen[s.day] = s

        # rest between consecutive shifts (rule 8)
        ordered = sorted(days_seen.values(), key=lambda s: s.start)
        pairs = list(zip(ordered, ordered[1:]))
        if ds.schedule_type == "CYCLIC" and ordered:
            # the pattern repeats: last shift of the horizon runs into the first of the next cycle
            first_next = ordered[0]
            wrapped = Shift(first_next.resource, emp, first_next.day + timedelta(days=7 * n_weeks),
                            first_next.start + timedelta(days=7 * n_weeks),
                            first_next.end + timedelta(days=7 * n_weeks), first_next.length_min, first_next.row)
            pairs.append((ordered[-1], wrapped))
        for a, b in pairs:
            gap = (b.start - a.end).total_seconds() / 60
            if gap < rules.rest_minutes:
                v.append(Violation("Minimum rest", f"only {gap / 60:g} h rest between shift on {a.day} ending {a.end:%a %H:%M} "
                                      f"and shift starting {b.start:%a %H:%M} (minimum {rules.rest_minutes / 60:g} h)",
                                   resource=res, employee=emp, date=b.day.isoformat()))

        # weekly rules (5, 6, 7), calendar day off (9)
        work_days = set(days_seen)
        week_patterns = []
        for w in range(n_weeks):
            wk_days = [first_day + timedelta(days=7 * w + k) for k in range(7)]
            wk_label = wk_days[0].isoformat()
            where = dict(resource=res, employee=emp, date=f"week of {wk_label}")
            minutes = sum(days_seen[d].length_min for d in wk_days if d in days_seen)
            if minutes != rules.weekly_minutes:
                v.append(Violation("Weekly hours", f"{minutes / 60:g} h worked; weekly target is exactly "
                                      f"{rules.weekly_minutes / 60:g} h", **where))
            off = [k for k, d in enumerate(wk_days) if d not in work_days]
            if not (rules.min_days_off <= len(off) <= rules.max_days_off):
                v.append(Violation("Days off", f"{len(off)} day(s) off; allowed {rules.min_days_off}–{rules.max_days_off}", **where))
            if rules.consecutive_day_off and off and (off[-1] - off[0] + 1) != len(off):
                v.append(Violation("Consecutive days off", "days off are not one unbroken run", **where))
            week_patterns.append(tuple(
                (k, days_seen[d].start.strftime("%H:%M"), days_seen[d].length_min)
                for k, d in enumerate(wk_days) if d in days_seen))

        if rules.calendar_day_off:
            for s in days_seen.values():
                midnight = datetime.combine(s.day + timedelta(days=1), datetime.min.time())
                nxt = s.day + timedelta(days=1)
                if s.end > midnight:
                    if nxt > last_day:
                        if ds.schedule_type != "CYCLIC":
                            continue
                        nxt = nxt - timedelta(days=7 * n_weeks)
                    if nxt not in work_days:
                        v.append(Violation("Calendar day off", f"shift ends {s.end:%a %H:%M}, running past midnight into a day off",
                                           resource=res, employee=emp, date=s.day.isoformat()))

        if ds.schedule_type == "CYCLIC" and len(set(week_patterns)) > 1:
            v.append(Violation("Cyclic pattern", "schedule is CYCLIC but the weekly pattern changes between weeks",
                               resource=res, employee=emp))

    # ---- assignments ------------------------------------------------------ #
    assigned: dict[tuple[int, int], set[str]] = defaultdict(set)
    tasks_by_emp: dict[str, list[Task]] = defaultdict(list)
    seen_pairs: set[tuple[str, tuple[int, int]]] = set()
    for i, r in enumerate(assign_rows, start=2):
        res, emp = r["resource"], r["employee_id"]
        where = dict(resource=res, employee=emp)
        try:
            key = (int(float(r["unique_turn_id"])), int(float(r["task_id"])))
        except ValueError:
            v.append(Violation("File format", f"assignments.csv line {i}: bad unique_turn_id / task_id", **where))
            continue
        task = ds.tasks.get(key)
        if task is None:
            v.append(Violation("Unknown task", f"assignments.csv line {i}: task (turn {key[0]}, id {key[1]}) is not a task "
                                   f"that starts inside the planning horizon", **where))
            continue
        where["date"] = task.start.date().isoformat()
        if (emp, key) in seen_pairs:
            v.append(Violation("File format", f"assignments.csv line {i}: employee assigned to task (turn {key[0]}, id {key[1]}) twice",
                               **where))
            continue
        seen_pairs.add((emp, key))
        if emp not in emp_resource:
            v.append(Violation("Task inside a shift", f"task (turn {key[0]}, id {key[1]}) assigned to an employee with no shifts", **where))
            continue
        if res != task.resource or emp_resource[emp] != task.resource:
            v.append(Violation("File format", f"task (turn {key[0]}, id {key[1]}) is a {task.resource} task; "
                                  f"row says {res}, employee is {emp_resource[emp]}", **where))
            continue
        inside = any(s.start <= task.start and s.end >= task.end for s in shifts_by_emp[emp])
        if not inside:
            v.append(Violation("Task inside a shift", f"task (turn {key[0]}, id {key[1]}) {task.start:%a %H:%M}–{task.end:%H:%M} "
                                   f"is not inside one of the employee's shifts", **where))
            continue
        tasks_by_emp[emp].append(task)
        assigned[key].add(emp)

    for emp, ts in tasks_by_emp.items():
        ts.sort(key=lambda t: (t.start, t.end))
        for a, b in zip(ts, ts[1:]):
            if b.start < a.end:
                v.append(Violation("One task at a time", f"tasks (turn {a.key[0]}, id {a.key[1]}) and (turn {b.key[0]}, id {b.key[1]}) "
                                       f"overlap", resource=emp_resource[emp], employee=emp,
                                   date=b.start.date().isoformat()))

    # ---- scoring ---------------------------------------------------------- #
    by_resource: dict[str, dict[str, int]] = defaultdict(lambda: {"tasks_total": 0, "tasks_covered": 0, "headcount": 0})
    covered = 0
    over = 0
    for key, t in ds.tasks.items():
        n = len(assigned.get(key, ()))
        by_resource[t.resource]["tasks_total"] += 1
        if n >= t.need:
            covered += 1
            by_resource[t.resource]["tasks_covered"] += 1
        if n > t.need:
            over += 1
    for emp, res in emp_resource.items():
        by_resource[res]["headcount"] += 1
    if over:
        warnings.append(f"{over} task(s) have more people than they need; the extra people earn nothing.")

    total = len(ds.tasks)
    return Result(
        valid=not v,
        coverage=(covered / total) if total else 0.0,
        tasks_covered=covered,
        tasks_total=total,
        headcount=len(emp_resource),
        violations=v,
        warnings=warnings,
        by_resource=dict(by_resource),
    )


# --------------------------------------------------------------------------- #
def main(argv: list[str]) -> int:
    if len(argv) != 5:
        print(__doc__)
        return 2
    ds = load_dataset(argv[1], argv[2])
    res = validate(ds, argv[3], argv[4])
    print(json.dumps(res.summary(), indent=2))
    for w in res.warnings:
        print("WARNING:", w)
    for x in res.violations[:200]:
        d = x.as_dict()
        print(f"[{d['rule']}] {d['resource']} {d['employee']} {d['date']}: {d['message']}")
    if len(res.violations) > 200:
        print(f"... and {len(res.violations) - 200} more")
    return 0 if res.valid else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
