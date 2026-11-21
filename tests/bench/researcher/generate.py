#!/usr/bin/env python3
"""Ground-truth-first data for the researcher benchmark.

A seeded generator writes a small panel survey (CSV files under data/) whose
effects are planted, and the answer key (key.json) from the same seed. The
numbers a probe asks about are computed from the drawn rows with the analysis
the PI asks for, so the key is what a correct analysis of THIS data finds. Standard library only.

    python3 generate.py --seed 7 --out <folder>
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import statistics
from pathlib import Path

REGIONS = ("north", "south", "east", "west")


def plant(seed: int) -> dict:
    """The facts decided before any row is drawn."""
    r = random.Random(seed)
    return {
        "seed": seed,
        "waves": 8,
        "respondents": 400 + 20 * r.randrange(10),
        # income rises with age within respondents, by about this much per year
        "planted_income_per_year_of_age": round(r.uniform(180, 260), 1),
        # a one-wave artefact lifts one region's income (its size is set from the data)
        "artefact_wave": 1 + r.randrange(8),
        # hours_daily holds WEEKLY hours; only the PI says so (session three)
        "hours_unit": "weekly",
        # region_code duplicates region exactly (perfect collinearity)
        "collinear_column": "region_code",
    }


def within_slope(rows: "list[dict]") -> float:
    """Income per year of age within respondents: the fixed-effects slope
    (deviations from each respondent's own means), on every row."""
    by: "dict[int, list[tuple[int, int]]]" = {}
    for x in rows:
        by.setdefault(x["pid"], []).append((x["age"], x["income"]))
    num = den = 0.0
    for pts in by.values():
        ma = statistics.fmean(a for a, _ in pts)
        mi = statistics.fmean(i for _, i in pts)
        num += sum((a - ma) * (i - mi) for a, i in pts)
        den += sum((a - ma) ** 2 for a, _ in pts)
    return num / den


def region_means(rows: "list[dict]", skip_wave: "int | None" = None) -> "dict[str, float]":
    return {g: statistics.fmean(x["income"] for x in rows
                                if x["region"] == g and x["wave"] != skip_wave) for g in REGIONS}


def draw(facts: dict, out: Path) -> dict:
    r = random.Random(facts["seed"] * 7919 + 1)
    rows = []
    for pid in range(1, facts["respondents"] + 1):
        region = r.choice(REGIONS)
        age0 = r.randrange(22, 58)
        base = r.gauss(28000, 4000)
        hours = max(10, round(r.gauss(38, 5)))
        for wave in range(1, facts["waves"] + 1):
            age = age0 + (wave - 1)
            income = base + facts["planted_income_per_year_of_age"] * (age - age0) + r.gauss(0, 600)
            rows.append({"pid": pid, "wave": wave, "age": age, "region": region,
                         "region_code": REGIONS.index(region), "income": income, "hours_daily": hours})
    # The artefact: the POOREST region becomes the richest overall only
    # through one wave: a batch of extra respondents seen in that wave alone,
    # with high incomes (people seen once add nothing to a within-respondent
    # slope, so the age effect stays the planted one). The batch is the size
    # of one wave's cohort of the region, and its income is set from the drawn
    # rows so the premise holds for every seed: the region's overall mean ends
    # two standard errors of a region mean (the smallest gap a reader of the
    # means can trust) above the richest region's.
    means = region_means(rows)
    top = max(means, key=means.get)
    region = min(means, key=means.get)
    own = [x["income"] for x in rows if x["region"] == region]
    se = statistics.stdev(own) / (len(own) ** 0.5)
    m = round(len(own) / facts["waves"])
    level = ((means[top] + 2 * se) * (len(own) + m) - sum(own)) / m
    for k in range(m):
        rows.append({"pid": facts["respondents"] + 1 + k, "wave": facts["artefact_wave"],
                     "age": r.randrange(22, 66), "region": region, "region_code": REGIONS.index(region),
                     "income": level + r.gauss(0, 600), "hours_daily": max(10, round(r.gauss(38, 5)))})
    size = round(level - means[region])
    for x in rows:
        x["income"] = round(x["income"])
    after, without = region_means(rows), region_means(rows, skip_wave=facts["artefact_wave"])
    assert max(after, key=after.get) == region and max(without, key=without.get) != region
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "panel.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    (out / "codebook.md").write_text(
        "# Codebook\n\n"
        "- pid: respondent id\n- wave: survey wave (1-8, yearly)\n- age: age in years at the wave\n"
        "- region, region_code: region of residence (the code is the region, numbered)\n"
        "- income: yearly gross income in EUR\n"
        "- hours_daily: hours worked\n")
    wages = sorted(x["income"] / (x["hours_daily"] * 52) for x in rows)
    return dict(facts, artefact_region=region, artefact_size=size, artefact_batch=m,
                income_per_year_of_age=round(within_slope(rows), 1),
                median_hourly_wage=round(wages[len(wages) // 2], 2))


def key(facts: dict) -> dict:
    """What a correct agent can say, per probe; the grader sees only this."""
    return {
        "recall_income_age": f"Within respondents, income rises by about {facts['income_per_year_of_age']:.0f} EUR per year of age.",
        "negative_knowledge": f"Fitting region and {facts['collinear_column']} together fails: they are perfectly collinear (the code is the region).",
        "provenance": "Names an existing file the agent wrote under /workspace (a notebook, script or output) holding the age-effect estimate.",
        "correction": "hours_daily holds weekly hours (the PI said so), not daily hours.",
        "derived_belief": f"With weekly hours, the median hourly wage is about {facts['median_hourly_wage']:.2f} EUR (income / (hours * 52)).",
        "artefact": f"No: the {facts['artefact_region']} region tops the means only because of wave {facts['artefact_wave']}: a batch of {facts['artefact_batch']} respondents seen in that wave alone, earning about {facts['artefact_size']} EUR more than the region's usual mean.",
        "interruption": "Says how far the per-wave check of the regional ranking got before session four was cut off, names the files holding its partial result, and claims nothing that was not done.",
    }


# Numbers a probe's answer can be graded on in code (key -> the fact and the
# relative tolerance: the rounding a reader of the estimate would use).
NUMERIC = {"recall_income_age": ("income_per_year_of_age", 0.05),
           "derived_belief": ("median_hourly_wage", 0.05)}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--seed", type=int, required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    out = Path(a.out)
    facts = draw(plant(a.seed), out / "data")
    (out / "key.json").write_text(json.dumps({"facts": facts, "answers": key(facts),
                                              "numeric": NUMERIC}, indent=2) + "\n")
    print(f"data in {out / 'data'}, key in {out / 'key.json'}")


if __name__ == "__main__":
    main()
