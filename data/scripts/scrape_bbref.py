#!/usr/bin/env python3
"""Scrape one NBA season of player salaries from Basketball Reference.

Pulls each team's season page (/teams/{ABBR}/{YEAR}.html), parses the
"Salaries" table, and writes a treemap-ready JSON snapshot to
data/nba/{season}.json.

Design / etiquette:
  * Polite: one request every REQUEST_DELAY seconds, real User-Agent.
  * Cached: raw HTML is saved under data/raw/ (gitignored) so re-runs and
    debugging never re-hit the site. Use --refresh to force re-download.
  * The site is NOT scraped in CI — this is a local, manual, ~once-a-season job.

Usage:
  python data/scripts/scrape_bbref.py --season 2025-26
  python data/scripts/scrape_bbref.py --season 2025-26 --refresh
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup, Comment

# data/ (this file lives in data/scripts/)
DATA_DIR = Path(__file__).resolve().parents[1]
NBA_DIR = DATA_DIR / "nba"
RAW_DIR = DATA_DIR / "raw" / "nba"

BASE = "https://www.basketball-reference.com"
USER_AGENT = (
    "salary-cap-viz/0.1 (personal data-viz project; "
    "contact: raul.murguia@gmail.com)"
)
REQUEST_DELAY = 3.5  # seconds between requests; BBR throttles aggressive scrapers


def season_end_year(season: str) -> int:
    """'2025-26' -> 2026 (BBR pages are keyed by the season's ending year)."""
    m = re.fullmatch(r"(\d{4})-(\d{2})", season)
    if not m:
        raise ValueError(f"season must look like '2025-26', got {season!r}")
    start = int(m.group(1))
    return start + 1


def load_teams() -> list[dict]:
    with open(NBA_DIR / "teams.json", encoding="utf-8") as fh:
        return json.load(fh)["teams"]


def fetch_team_html(abbr: str, end_year: int, *, refresh: bool) -> str:
    """Return the team-season page HTML, from cache if available."""
    cache = RAW_DIR / str(end_year) / f"{abbr}.html"
    if cache.exists() and not refresh:
        return cache.read_text(encoding="utf-8")

    url = f"{BASE}/teams/{abbr}/{end_year}.html"
    print(f"  GET {url}", file=sys.stderr)
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()
    resp.encoding = "utf-8"  # BBR serves UTF-8; don't let requests guess (mangles é, ć, etc.)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(resp.text, encoding="utf-8")
    time.sleep(REQUEST_DELAY)  # only sleep on a real network hit
    return resp.text


def find_salaries_table(soup: BeautifulSoup):
    """Locate the salaries table.

    BBR hides several tables inside HTML comments to deter scrapers, so we
    search both the live DOM and comment blocks.
    """
    table = soup.find("table", id="salaries2")
    if table is not None:
        return table
    for comment in soup.find_all(string=lambda t: isinstance(t, Comment)):
        if "salaries2" in comment:
            inner = BeautifulSoup(comment, "lxml")
            table = inner.find("table", id="salaries2")
            if table is not None:
                return table
    return None


def parse_salary(text: str) -> int | None:
    """'$51,915,615' -> 51915615 ; blanks/non-numeric -> None."""
    digits = re.sub(r"[^0-9]", "", text or "")
    return int(digits) if digits else None


def parse_players(table) -> list[dict]:
    players: list[dict] = []
    body = table.find("tbody") or table
    for row in body.find_all("tr"):
        if "thead" in (row.get("class") or []):
            continue
        name_cell = row.find(attrs={"data-stat": "player"})
        salary_cell = row.find(attrs={"data-stat": "salary"})
        if name_cell is None or salary_cell is None:
            continue
        name = name_cell.get_text(strip=True)
        salary = parse_salary(salary_cell.get_text(strip=True))
        if not name or salary is None or salary <= 0:
            continue
        players.append({"name": name, "salary": salary})
    return players


def merge_same_player_rows(players: list[dict]) -> list[dict]:
    """Sum multiple line items for the same player on one team.

    BBR's salary table can list a player on several rows (10-day deals,
    dead money from a waived contract, etc.). For a single team those are
    the same person and should be one cell summing to their total cap hit.
    (Cross-team duplicates from trades are intentionally kept separate —
    each team really did carry that portion.)
    """
    merged: dict[str, dict] = {}
    order: list[str] = []
    for p in players:
        if p["name"] in merged:
            merged[p["name"]]["salary"] += p["salary"]
        else:
            merged[p["name"]] = {"name": p["name"], "salary": p["salary"]}
            order.append(p["name"])
    return [merged[n] for n in order]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--season", required=True, help="e.g. 2025-26")
    ap.add_argument("--refresh", action="store_true",
                    help="force re-download instead of using cached HTML")
    args = ap.parse_args()

    end_year = season_end_year(args.season)
    teams = load_teams()

    conferences: dict[str, list[dict]] = {"Eastern": [], "Western": []}
    grand_total = 0

    for team in teams:
        abbr = team["bbref"]
        print(f"[{abbr}] {team['name']}", file=sys.stderr)
        html = fetch_team_html(abbr, end_year, refresh=args.refresh)
        table = find_salaries_table(BeautifulSoup(html, "lxml"))
        if table is None:
            print(f"  !! no salaries table found for {abbr}", file=sys.stderr)
            players = []
        else:
            players = parse_players(table)
        players = merge_same_player_rows(players)
        payroll = sum(p["salary"] for p in players)
        grand_total += payroll
        conferences[team["conference"]].append({
            "bbref": abbr,
            "name": team["name"],
            "division": team["division"],
            "payroll": payroll,
            "players": players,
        })
        print(f"  {len(players)} players, ${payroll:,}", file=sys.stderr)

    snapshot = {
        "season": args.season,
        "sport": "NBA",
        "source": "Basketball Reference (basketball-reference.com)",
        "sourceNote": "Player salaries scraped from team season pages. "
                      "Aggregated for a personal, non-commercial visualization.",
        "retrieved": dt.date.today().isoformat(),
        "totalPayroll": grand_total,
        "conferences": [
            {"name": "Eastern", "teams": conferences["Eastern"]},
            {"name": "Western", "teams": conferences["Western"]},
        ],
    }

    out = NBA_DIR / f"{args.season}.json"
    out.write_text(json.dumps(snapshot, indent=2) + "\n", encoding="utf-8")
    n_players = sum(len(t["players"])
                    for c in snapshot["conferences"] for t in c["teams"])
    print(f"\nWrote {out} — {n_players} players, "
          f"total ${grand_total:,}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
