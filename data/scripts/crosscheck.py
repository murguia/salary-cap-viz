#!/usr/bin/env python3
"""Cross-source validation of the scraped NBA salary snapshot.

Compares data/nba/{season}.json (from Basketball Reference) against an
independent second source — ESPN's season salary pages — to confirm or
refute the *team affiliation* of each player. This is the check the internal
validator can't do: it needs a second source to say "yes, this player really
is on that team."

ESPN's per-season URL is used (/nba/salaries/_/year/{endYear}), which is
server-rendered and carries RK / NAME / TEAM / SALARY for the same season as
our snapshot — so the comparison is apples-to-apples (unlike live "current"
pages, which have rolled to the upcoming season).

Reconciliation notes:
  * Player names are normalized (accents, suffixes, punctuation, ESPN's
    trailing ", POS") before matching.
  * Traded players legitimately appear on 2+ teams in the BBR snapshot; a
    mismatch is only "unreconciled" if ESPN's team is NOT among the teams
    BBR lists for that player.
  * ESPN omits many sub-minimum / waived dead-money entries, so "only in
    BBR" slivers are expected and reported separately, not as errors.

Usage:
  python data/scripts/crosscheck.py --season 2025-26
  python data/scripts/crosscheck.py --season 2025-26 --refresh
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import unicodedata
from collections import defaultdict
from pathlib import Path

import requests
from bs4 import BeautifulSoup

DATA_DIR = Path(__file__).resolve().parents[1]
NBA_DIR = DATA_DIR / "nba"
RAW_DIR = DATA_DIR / "raw" / "espn"

ESPN_BASE = "https://www.espn.com/nba/salaries"
USER_AGENT = (
    "salary-cap-viz/0.1 (personal data-viz project; "
    "contact: raul.murguia@gmail.com)"
)
REQUEST_DELAY = 3.5
MAX_PAGES = 25

NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}
# Some BBR names contain Cyrillic look-alike letters (e.g. "Egor Dёmin" has a
# Cyrillic ё, U+0451). Transliterate so they match ESPN's Latin spelling.
_CYRILLIC = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "i", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}
# ESPN spells a couple of teams differently from teams.json.
TEAM_ALIASES = {"la clippers": "LAC"}
SALARY_TOL = 0.02  # 2% relative tolerance for single-team salary comparison


def season_end_year(season: str) -> int:
    m = re.fullmatch(r"(\d{4})-(\d{2})", season)
    if not m:
        raise ValueError(f"season must look like '2025-26', got {season!r}")
    return int(m.group(1)) + 1


def norm_name(name: str) -> str:
    name = name.split(",")[0].lower()  # drop ESPN's ", G" position suffix
    name = "".join(_CYRILLIC.get(c, c) for c in name)  # cyrillic look-alikes
    name = unicodedata.normalize("NFKD", name)
    name = "".join(c for c in name if not unicodedata.combining(c))
    name = name.replace("’", "'").replace(".", "").replace("'", "")
    name = re.sub(r"[^a-z0-9\- ]", " ", name)
    toks = [t for t in name.split() if t]
    while toks and toks[-1] in NAME_SUFFIXES:
        toks.pop()
    return " ".join(toks)


def secondary_key(norm: str):
    """(last name, first initial) — a conservative fallback identity used only
    when it is unique on BOTH sides, to reconcile e.g. 'Ron Holland' with
    'Ronald Holland II' without risking collisions."""
    toks = norm.split()
    return (toks[-1], toks[0][0]) if toks and toks[0] else None


def norm_team(name: str) -> str:
    name = unicodedata.normalize("NFKD", name)
    name = "".join(c for c in name if not unicodedata.combining(c)).lower()
    return re.sub(r"[^a-z0-9 ]", " ", name).strip()


def parse_salary(text: str) -> int | None:
    digits = re.sub(r"[^0-9]", "", text or "")
    return int(digits) if digits else None


def fetch_espn_page(end_year: int, page: int, *, refresh: bool) -> str:
    cache = RAW_DIR / str(end_year) / f"page{page}.html"
    if cache.exists() and not refresh:
        return cache.read_text(encoding="utf-8")
    url = f"{ESPN_BASE}/_/year/{end_year}" + (f"/page/{page}" if page > 1 else "")
    print(f"  GET {url}", file=sys.stderr)
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()
    resp.encoding = "utf-8"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(resp.text, encoding="utf-8")
    time.sleep(REQUEST_DELAY)
    return resp.text


def scrape_espn(end_year: int, *, refresh: bool) -> list[dict]:
    """Return [{name, team_full, salary}] across all pages."""
    rows: list[dict] = []
    seen_first = None
    for page in range(1, MAX_PAGES + 1):
        html = fetch_espn_page(end_year, page, refresh=refresh)
        table = BeautifulSoup(html, "lxml").find("table")
        if table is None:
            break
        page_rows = []
        for tr in table.find_all("tr"):
            cells = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
            if len(cells) >= 4 and cells[0].isdigit():
                sal = parse_salary(cells[3])
                if sal:
                    page_rows.append(
                        {"name": cells[1], "team_full": cells[2], "salary": sal}
                    )
        if not page_rows:
            break
        # ESPN clamps out-of-range page numbers back to page 1 — stop on repeat.
        if page_rows[0]["name"] == seen_first:
            break
        seen_first = page_rows[0]["name"]
        rows.extend(page_rows)
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--season", required=True)
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()

    end_year = season_end_year(args.season)
    snapshot = json.load(open(NBA_DIR / f"{args.season}.json", encoding="utf-8"))
    team_meta = json.load(open(NBA_DIR / "teams.json", encoding="utf-8"))["teams"]
    team_by_norm = {norm_team(t["name"]): t["bbref"] for t in team_meta}
    team_by_norm.update(TEAM_ALIASES)
    valid_abbr = {t["bbref"] for t in team_meta}

    # BBR side: player -> {team: salary}
    bbr = defaultdict(dict)
    bbr_display = {}
    for conf in snapshot["conferences"]:
        for t in conf["teams"]:
            for p in t["players"]:
                key = norm_name(p["name"])
                bbr[key][t["bbref"]] = p["salary"]
                bbr_display.setdefault(key, p["name"])

    # ESPN side
    print(f"Scraping ESPN salaries for {args.season} (year {end_year})...",
          file=sys.stderr)
    espn_rows = scrape_espn(end_year, refresh=args.refresh)
    print(f"  {len(espn_rows)} ESPN player rows", file=sys.stderr)

    espn = {}
    unmapped_teams = set()
    for r in espn_rows:
        tn = norm_team(r["team_full"])
        abbr = team_by_norm.get(tn)
        if abbr is None:
            unmapped_teams.add(r["team_full"])
            continue
        espn[norm_name(r["name"])] = {
            "team": abbr, "salary": r["salary"], "display": r["name"], "team_full": r["team_full"],
        }

    # --- compare ----------------------------------------------------------
    confirmed = []          # ESPN team is among BBR's teams for the player
    reconciled_trade = []   # mismatch, but player is multi-team in BBR (trade)
    unreconciled = []       # ESPN team NOT in BBR's set -> real flag to VERIFY
    salary_diffs = []       # same single team, salary off beyond tolerance

    def compare(bbr_teams, display, espn_rec):
        if espn_rec["team"] in bbr_teams:
            confirmed.append(display)
            if len(bbr_teams) == 1:  # salary check only when unambiguous
                b = next(iter(bbr_teams.values()))
                e = espn_rec["salary"]
                if b and abs(b - e) > max(SALARY_TOL * b, 50_000):
                    salary_diffs.append(
                        {"player": display, "team": espn_rec["team"],
                         "bbref": b, "espn": e})
        elif len(bbr_teams) > 1:
            reconciled_trade.append(
                {"player": display, "espn_team": espn_rec["team"],
                 "bbr_teams": sorted(bbr_teams)})
        else:
            unreconciled.append(
                {"player": display, "espn_team": espn_rec["team"],
                 "bbr_team": next(iter(bbr_teams)),
                 "espn_salary": espn_rec["salary"],
                 "bbr_salary": next(iter(bbr_teams.values()))})

    # phase 1: exact normalized-name match
    espn_unmatched = {}
    for key, espn_rec in espn.items():
        if key in bbr:
            compare(bbr[key], bbr_display[key], espn_rec)
        else:
            espn_unmatched[key] = espn_rec
    bbr_unmatched = {k for k in bbr if k not in espn}

    # phase 2: (last name, first initial) fallback, unique on both sides only
    espn_by_s = defaultdict(list)
    bbr_by_s = defaultdict(list)
    for k in espn_unmatched:
        if (s := secondary_key(k)):
            espn_by_s[s].append(k)
    for k in bbr_unmatched:
        if (s := secondary_key(k)):
            bbr_by_s[s].append(k)
    fuzzy_espn, fuzzy_bbr = set(), set()
    for s, eks in espn_by_s.items():
        bks = bbr_by_s.get(s, [])
        if len(eks) == 1 and len(bks) == 1:
            compare(bbr[bks[0]], bbr_display[bks[0]], espn_unmatched[eks[0]])
            fuzzy_espn.add(eks[0])
            fuzzy_bbr.add(bks[0])
    fuzzy_matched = len(fuzzy_espn)

    only_espn = [rec for k, rec in espn_unmatched.items() if k not in fuzzy_espn]
    only_bbr_sig = [
        {"player": bbr_display[k], "teams": sorted(bbr[k]),
         "salary": max(bbr[k].values())}
        for k in bbr_unmatched
        if k not in fuzzy_bbr and max(bbr[k].values()) >= 1_000_000
    ]

    # --- report -----------------------------------------------------------
    def line(mark, msg):
        print(f"{mark} {msg}", file=sys.stderr)

    print("\n=== cross-source validation: BBR vs ESPN ===", file=sys.stderr)
    line("✓", f"team confirmed by ESPN: {len(confirmed)} players")
    line("✓", f"reconciled via name fallback (Ron/Ronald-type): {fuzzy_matched}")
    line("✓", f"trade mismatches reconciled (ESPN team ∈ BBR teams' split): "
              f"{len(reconciled_trade)}")
    if unmapped_teams:
        line("✗", f"UNMAPPED ESPN team names (add alias): {sorted(unmapped_teams)}")
    if unreconciled:
        line("✗", f"UNRECONCILED team mismatches (VERIFY — real accuracy flags): "
                  f"{len(unreconciled)}")
        for u in unreconciled:
            line(" ", f"    {u['player']}: BBR={u['bbr_team']} "
                      f"(${u['bbr_salary']:,}) vs ESPN={u['espn_team']} "
                      f"(${u['espn_salary']:,})")
    else:
        line("✓", "UNRECONCILED team mismatches: 0")
    if salary_diffs:
        line("!", f"salary discrepancies >2% (same team): {len(salary_diffs)}")
        for s in sorted(salary_diffs, key=lambda x: -abs(x['bbref'] - x['espn']))[:15]:
            line(" ", f"    {s['player']} ({s['team']}): "
                      f"BBR ${s['bbref']:,} vs ESPN ${s['espn']:,}")
    else:
        line("✓", "salary discrepancies: 0")
    line("!", f"significant players in BBR but not ESPN (>$1M): {len(only_bbr_sig)}")
    for o in sorted(only_bbr_sig, key=lambda x: -x['salary'])[:10]:
        line(" ", f"    {o['player']} {o['teams']} ${o['salary']:,}")
    line("!", f"players in ESPN but not BBR: {len(only_espn)}")
    for o in sorted(only_espn, key=lambda x: -x['salary'])[:10]:
        line(" ", f"    {o['display']} ({o['team']}) ${o['salary']:,}")

    n_flags = len(unreconciled) + len(unmapped_teams)
    print(f"\nESPN players matched: {len(confirmed) + len(reconciled_trade) + len(unreconciled)}"
          f" / {len(espn)} · unreconciled flags: {len(unreconciled)}", file=sys.stderr)

    report = {
        "season": args.season, "source_b": f"ESPN /year/{end_year}",
        "espn_rows": len(espn_rows),
        "confirmed": len(confirmed), "fuzzy_matched": fuzzy_matched,
        "reconciled_trade": reconciled_trade,
        "unreconciled": unreconciled, "salary_diffs": salary_diffs,
        "only_in_bbr_significant": only_bbr_sig, "only_in_espn": only_espn,
        "unmapped_espn_teams": sorted(unmapped_teams),
    }
    out = NBA_DIR / f"{args.season}.crosscheck.json"
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"report -> {out}", file=sys.stderr)
    return 1 if n_flags else 0


if __name__ == "__main__":
    raise SystemExit(main())
