"""Leaderboard ranking from stored results.

Per dataset: each team's best valid result, ranked by coverage (higher wins),
then headcount (lower wins), then submission time (earlier wins; the portal
takes output files only, so run time isn't measured).

Overall: every active dataset counts; a dataset with no valid result counts as
0% coverage. Ranked by average coverage, then total headcount over the datasets
the team qualified on, then the time of the team's latest counted result.
"""

from __future__ import annotations

import pandas as pd


def _best_per_team(results: list[dict], dataset: str) -> pd.DataFrame:
    rows = [r for r in results if r["dataset"] == dataset and r["valid"]]
    if not rows:
        return pd.DataFrame(columns=["team", "coverage", "tasks_covered", "tasks_total", "headcount",
                                     "submitted_at", "submission_id"])
    df = pd.DataFrame(rows)
    df = df.sort_values(["coverage", "headcount", "submitted_at"], ascending=[False, True, True])
    return df.drop_duplicates("team", keep="first")


def per_dataset(results: list[dict], dataset: str) -> pd.DataFrame:
    df = _best_per_team(results, dataset).reset_index(drop=True)
    out = pd.DataFrame({
        "Rank": range(1, len(df) + 1),
        "Team": df["team"],
        "Coverage %": (df["coverage"] * 100).round(2),
        "Tasks covered": df["tasks_covered"].astype(str) + " / " + df["tasks_total"].astype(str),
        "Headcount": df["headcount"],
        "Submitted": df["submitted_at"].str.slice(0, 16).str.replace("T", " "),
        "Submission #": df["submission_id"],
    })
    return out


def overall(results: list[dict], datasets: list[str]) -> pd.DataFrame:
    teams = sorted({r["team"] for r in results})
    rows = []
    for team in teams:
        covs, heads, qualified, last = [], 0, 0, ""
        for ds in datasets:
            best = _best_per_team([r for r in results if r["team"] == team], ds)
            if len(best):
                b = best.iloc[0]
                covs.append(float(b["coverage"]))
                heads += int(b["headcount"])
                qualified += 1
                last = max(last, b["submitted_at"])
            else:
                covs.append(0.0)
        rows.append({"team": team, "avg": sum(covs) / len(covs) if covs else 0.0, "heads": heads,
                     "qualified": qualified, "last": last, **{f"cov_{d}": c for d, c in zip(datasets, covs)}})
    if not rows:
        return pd.DataFrame(columns=["Rank", "Team", "Average coverage %", "Total headcount", "Datasets qualified"])
    df = pd.DataFrame(rows).sort_values(["avg", "heads", "last"], ascending=[False, True, True]).reset_index(drop=True)
    out = pd.DataFrame({
        "Rank": range(1, len(df) + 1),
        "Team": df["team"],
        "Average coverage %": (df["avg"] * 100).round(2),
        "Total headcount": df["heads"],
        "Datasets qualified": df["qualified"].astype(str) + f" / {len(datasets)}",
    })
    for d in datasets:
        out[f"{d} cov %"] = (df[f"cov_{d}"] * 100).round(2)
    return out
