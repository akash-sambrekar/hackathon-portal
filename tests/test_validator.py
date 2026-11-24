import csv
import io
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import validator  # noqa: E402

DS = os.path.join(ROOT, "sample_data", "dataset_A")
DSB = os.path.join(ROOT, "sample_data", "dataset_B")


def _rows(path):
    with open(path) as f:
        return list(csv.reader(f))


def _csv(rows):
    buf = io.StringIO()
    csv.writer(buf).writerows(rows)
    return buf.getvalue()


@pytest.fixture(scope="module")
def ds():
    return validator.load_dataset(os.path.join(DS, "tasks.json"), os.path.join(DS, "config.json"))


@pytest.fixture
def shifts():
    return _rows(os.path.join(DS, "shifts.csv"))


@pytest.fixture
def assigns():
    return _rows(os.path.join(DS, "assignments.csv"))


def rules(res):
    return {x.rule for x in res.violations}


def test_baseline_valid(ds, shifts, assigns):
    r = validator.validate(ds, _csv(shifts), _csv(assigns))
    assert r.valid, [x.as_dict() for x in r.violations[:5]]
    assert 0 < r.coverage < 1  # the 10-hour task can never be covered
    assert r.headcount == len({row[1] for row in shifts[1:]})


def test_cyclic_baseline_valid():
    ds = validator.load_dataset(os.path.join(DSB, "tasks.json"), os.path.join(DSB, "config.json"))
    r = validator.validate(ds, os.path.join(DSB, "shifts.csv"), os.path.join(DSB, "assignments.csv"))
    assert r.valid and ds.schedule_type == "CYCLIC"


def test_empty_roster_is_valid_with_zero_coverage(ds):
    r = validator.validate(ds, ",".join(validator.SHIFT_COLUMNS) + "\n", ",".join(validator.ASSIGN_COLUMNS) + "\n")
    assert r.valid and r.coverage == 0 and r.headcount == 0


def test_missing_column(ds):
    with pytest.raises(validator.FormatError):
        validator.validate(ds, "resource,employee_id\nLDR,E1\n", ",".join(validator.ASSIGN_COLUMNS) + "\n")


def test_bad_length(ds, shifts, assigns):
    shifts[1][4] = "9"
    r = validator.validate(ds, _csv(shifts), _csv(assigns))
    assert "Shift length" in rules(r) and "Weekly hours" in rules(r)


def test_half_hour_lengths_for_cln(ds, shifts, assigns):
    assert 8 * 60 + 30 in ds.rules["CLN"].lengths_min
    assert 8 * 60 + 30 not in ds.rules["LDR"].lengths_min


def test_start_not_half_hour(ds, shifts, assigns):
    shifts[1][3] = "06:15"
    r = validator.validate(ds, _csv(shifts), _csv(assigns))
    assert "Shift start time" in rules(r)


def test_forbidden_start(ds, shifts, assigns):
    shifts[1][3] = "05:00"
    r = validator.validate(ds, _csv(shifts), _csv(assigns))
    assert "Forbidden start/end window" in rules(r)


def test_forbidden_end_window(ds, shifts, assigns):
    shifts[1][3] = "17:00"  # 8h -> ends 01:00, inside 23:01-02:59
    r = validator.validate(ds, _csv(shifts), _csv(assigns))
    assert "Forbidden start/end window" in rules(r)


def test_two_shifts_same_day(ds, shifts, assigns):
    shifts.append(list(shifts[1]))
    r = validator.validate(ds, _csv(shifts), _csv(assigns))
    assert "One shift per day" in rules(r)


def test_rest_violation(ds, shifts, assigns):
    emp = shifts[1][1]
    emp_rows = [r for r in shifts[1:] if r[1] == emp]
    emp_rows[1][3] = "22:00"  # previous day 06-14, this day 22:00 is fine; make the *next* day early
    emp_rows[1][3] = "14:00"
    emp_rows[2][3] = "06:00"  # 14:00-22:00 then 06:00 next day = 8h rest < 12h
    r = validator.validate(ds, _csv(shifts), _csv(assigns))
    assert "Minimum rest" in rules(r)


def test_days_off_and_consecutive(ds, shifts, assigns):
    # remove one CLN shift: hours drop and days off become 3
    idx = next(i for i, r in enumerate(shifts) if r[0] == "CLN")
    del shifts[idx]
    r = validator.validate(ds, _csv(shifts), _csv(assigns))
    assert {"Weekly hours", "Days off"} <= rules(r)


def test_task_outside_shift(ds, shifts, assigns):
    # move every shift of the first assigned employee to 14:00 on day of task -> task may fall outside
    emp = assigns[1][1]
    task = ds.tasks[(int(assigns[1][2]), int(assigns[1][3]))]
    for r in shifts[1:]:
        if r[1] == emp and r[2] == task.start.date().isoformat():
            r[3] = "14:00" if task.start.hour < 14 else "06:00"
    r = validator.validate(ds, _csv(shifts), _csv(assigns))
    assert "Task inside a shift" in rules(r)


def test_overlapping_tasks(ds, shifts, assigns):
    # find two overlapping tasks of the same resource and give both to one employee
    ts = sorted((t for t in ds.tasks.values() if t.resource == "LDR" and t.end.hour < 14 and t.start.hour >= 6),
                key=lambda t: t.start)
    pair = next((a, b) for a, b in zip(ts, ts[1:]) if b.start < a.end)
    emp = next(r[1] for r in shifts[1:] if r[0] == "LDR" and r[3] == "06:00"
               and all(any(s[1] == r[1] and s[2] == t.start.date().isoformat() for s in shifts[1:]) for t in pair))
    rows = [validator.ASSIGN_COLUMNS] + [["LDR", emp, t.key[0], t.key[1]] for t in pair]
    r = validator.validate(ds, _csv(shifts), _csv(rows))
    assert "One task at a time" in rules(r)


def test_unknown_task(ds, shifts, assigns):
    assigns.append(["LDR", assigns[1][1], "99999", "1"])
    r = validator.validate(ds, _csv(shifts), _csv(assigns))
    assert "Unknown task" in rules(r)


def test_partial_staffing_not_covered(ds, shifts, assigns):
    base = validator.validate(ds, _csv(shifts), _csv(assigns))
    multi = next(k for k, t in ds.tasks.items() if t.need > 1
                 and sum(1 for a in assigns[1:] if (int(a[2]), int(a[3])) == k) == t.need)
    rows = [a for a in assigns if not (a[0] != "resource" and (int(a[2]), int(a[3])) == multi)]
    rows += [next(a for a in assigns[1:] if (int(a[2]), int(a[3])) == multi)]
    r = validator.validate(ds, _csv(shifts), _csv(rows))
    assert r.valid and r.tasks_covered == base.tasks_covered - 1


def test_cyclic_pattern_change():
    ds = validator.load_dataset(os.path.join(DSB, "tasks.json"), os.path.join(DSB, "config.json"))
    sh = _rows(os.path.join(DSB, "shifts.csv"))
    emp = sh[1][1]
    rows = [r for r in sh[1:] if r[1] == emp]
    rows[-1][3] = "06:30"  # a week-2 shift moves by 30 minutes
    r = validator.validate(ds, _csv(sh), "resource,employee_id,unique_turn_id,task_id\n")
    assert "Cyclic pattern" in rules(r)


def test_calendar_day_off(ds):
    # CLN has calendar day off on. 9h shift starting 18:00 ends 03:00 next day (end 03:00 is allowed)
    rows = [validator.SHIFT_COLUMNS]
    # Mon-Fri work, Sat-Sun off; Friday shift runs into Saturday
    for w in range(2):
        for d in range(5):
            day = f"2026-11-{9 + 7 * w + d:02d}"
            rows.append(["CLN", "X1", day, "18:00" if d == 4 else "06:00", "8"])
    r = validator.validate(ds, _csv(rows), ",".join(validator.ASSIGN_COLUMNS) + "\n")
    assert "Calendar day off" in rules(r)
