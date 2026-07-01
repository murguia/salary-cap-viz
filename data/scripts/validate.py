#!/usr/bin/env python3
"""Validate a scraped NBA salary snapshot.

Runs a battery of internal-consistency checks against data/nba/{season}.json
and prints a report grouped by severity. Writes a machine-readable report to
data/nba/{season}.validation.json. Exits non-zero if any ERROR-level check
fails, so it can gate a commit / CI build of the site.

What it CAN do: catch structural problems (missing teams, duplicate rows,
impossible salaries, out-of-band totals) and surface things a human should
eyeball (mid-season trades, dead-money slivers).

What it CANNOT do: confirm that a given player is really on a given team.
That requires a trusted second source; those cases are reported as WARN
"verify", not asserted correct.

Usage:
  python data/scripts/validate.py --season 2025-26
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parents[1]
NBA_DIR = DATA_DIR / "nba"

# 2025-26 reference thresholds (official NBA figures).
SUPERMAX_CEILING = 62_000_000          # nobody earns above ~$60M this season
TEAM_PAYROLL_RANGE = (60_000_000, 430_000_000)
LEAGUE_TOTAL_RANGE = (3_800_000_000, 7_000_000_000)
SIGNIFICANT_SALARY = 1_000_000         # below this = minimum/dead-money sliver
SIGNIFICANT_ROSTER_RANGE = (7, 20)     # plausible count of >$1M players


class Report:
    def __init__(self) -> None:
        self.items: list[dict] = []

    def add(self, level: str, check: str, message: str, detail=None) -> None:
        self.items.append(
            {"level": level, "check": check, "message": message, "detail": detail}
        )

    def error(self, *a):
        self.add("ERROR", *a)

    def warn(self, *a):
        self.add("WARN", *a)

    def info(self, *a):
        self.add("INFO", *a)

    @property
    def n_errors(self) -> int:
        return sum(1 for i in self.items if i["level"] == "ERROR")


def load_expected_teams() -> dict[str, str]:
    teams = json.load(open(NBA_DIR / "teams.json", encoding="utf-8"))["teams"]
    return {t["bbref"]: t["conference"] for t in teams}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--season", required=True)
    args = ap.parse_args()

    path = NBA_DIR / f"{args.season}.json"
    data = json.load(open(path, encoding="utf-8"))
    rep = Report()

    expected = load_expected_teams()
    teams = [(c["name"], t) for c in data["conferences"] for t in c["teams"]]
    team_by_abbr = {t["bbref"]: (conf, t) for conf, t in teams}

    # --- structural: team roster completeness -----------------------------
    got = set(team_by_abbr)
    exp = set(expected)
    if got != exp:
        if exp - got:
            rep.error("teams", f"missing teams: {sorted(exp - got)}")
        if got - exp:
            rep.error("teams", f"unexpected teams: {sorted(got - exp)}")
    else:
        rep.info("teams", f"all {len(exp)} teams present")

    for abbr, (conf, t) in team_by_abbr.items():
        if abbr in expected and expected[abbr] != conf:
            rep.error("conference", f"{abbr} in {conf}, expected {expected[abbr]}")

    # --- duplicates -------------------------------------------------------
    where = defaultdict(list)
    for conf, t in teams:
        for p in t["players"]:
            where[(t["bbref"], p["name"])].append(p["salary"])
    same_team_dups = {k: v for k, v in where.items() if len(v) > 1}
    if same_team_dups:
        rep.error(
            "same-team-dupes",
            f"{len(same_team_dups)} players listed on multiple rows for one "
            f"team (should be merged)",
            [f"{tm}:{nm} x{len(v)}" for (tm, nm), v in same_team_dups.items()],
        )
    else:
        rep.info("same-team-dupes", "no duplicate rows within a team")

    cross = defaultdict(list)
    for conf, t in teams:
        for p in t["players"]:
            cross[p["name"]].append((t["bbref"], p["salary"]))
    traded = {n: v for n, v in cross.items() if len({tm for tm, _ in v}) > 1}
    if traded:
        rep.warn(
            "traded-players",
            f"{len(traded)} players carry cap hits on 2+ teams (mid-season "
            f"trades / dead money — verify these are intended)",
            {n: [f"{tm} ${s:,}" for tm, s in v] for n, v in sorted(traded.items())},
        )

    # --- salary sanity ----------------------------------------------------
    over = []
    nonpos = []
    for conf, t in teams:
        for p in t["players"]:
            if p["salary"] > SUPERMAX_CEILING:
                over.append(f"{p['name']} ({t['bbref']}) ${p['salary']:,}")
            if p["salary"] <= 0:
                nonpos.append(f"{p['name']} ({t['bbref']})")
    if over:
        rep.error("salary-ceiling",
                  f"{len(over)} salaries exceed ${SUPERMAX_CEILING:,}", over)
    if nonpos:
        rep.error("salary-nonpositive",
                  f"{len(nonpos)} non-positive salaries", nonpos)
    if not over and not nonpos:
        rep.info("salary-bounds", "all salaries within plausible bounds")

    # --- roster size / sliver profile ------------------------------------
    for conf, t in teams:
        sig = [p for p in t["players"] if p["salary"] >= SIGNIFICANT_SALARY]
        lo, hi = SIGNIFICANT_ROSTER_RANGE
        if not (lo <= len(sig) <= hi):
            rep.warn(
                "roster-size",
                f"{t['bbref']} has {len(sig)} players over "
                f"${SIGNIFICANT_SALARY/1e6:.0f}M (expected {lo}-{hi})",
                {"total_rows": len(t["players"]), "significant": len(sig)},
            )

    # --- payroll / league totals -----------------------------------------
    for conf, t in teams:
        lo, hi = TEAM_PAYROLL_RANGE
        if not (lo <= t["payroll"] <= hi):
            rep.warn("team-payroll",
                     f"{t['bbref']} payroll ${t['payroll']:,} outside "
                     f"${lo:,}-${hi:,}")
    total = data.get("totalPayroll", sum(t["payroll"] for _, t in teams))
    lo, hi = LEAGUE_TOTAL_RANGE
    if not (lo <= total <= hi):
        rep.error("league-total",
                  f"league total ${total:,} outside ${lo:,}-${hi:,}")
    else:
        rep.info("league-total", f"league total ${total:,} within range")

    # --- output -----------------------------------------------------------
    order = {"ERROR": 0, "WARN": 1, "INFO": 2}
    for it in sorted(rep.items, key=lambda i: order[i["level"]]):
        mark = {"ERROR": "✗", "WARN": "!", "INFO": "✓"}[it["level"]]
        print(f"{mark} [{it['level']:5}] {it['check']}: {it['message']}",
              file=sys.stderr)

    n_players = sum(len(t["players"]) for _, t in teams)
    print(f"\n{len(teams)} teams · {n_players} player rows · "
          f"{rep.n_errors} error(s)", file=sys.stderr)

    out = NBA_DIR / f"{args.season}.validation.json"
    out.write_text(json.dumps(
        {"season": args.season, "errors": rep.n_errors, "items": rep.items},
        indent=2) + "\n", encoding="utf-8")
    print(f"report -> {out}", file=sys.stderr)

    return 1 if rep.n_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
