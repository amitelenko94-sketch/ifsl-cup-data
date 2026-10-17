"""
IFSL Super League Cup — data collector.

Reads the PUBLIC Yahoo pages of all 19 IFSL leagues (no login, no API) and saves every
team's weekly category totals to cup-data.json. The website reads that file to build the
cup matchups (a team from one league vs a team from another, compared on the same week).

Runs automatically in GitHub Actions (see .github/workflows/cup-sync.yml), or locally:
    python cup_scrape.py
"""
import collections
import datetime
import json
import os
import re
import sys
import time

import requests
from bs4 import BeautifulSoup

BASE = "https://basketball.fantasysports.yahoo.com/nba/"
OUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cup-data.json")

# (yahoo league id, tier, name) — tier 1 = ליגת העל ... 5 = ליגה חמישית
LEAGUES = [
    (3109, 1, "Jordan"),
    (3130, 2, "Magic"), (3133, 2, "Bird"),
    (3137, 3, "Kobe"), (3140, 3, "Shaq"), (3149, 3, "Duncan"), (3156, 3, "Garnett"),
    (3159, 4, "Nash"), (3170, 4, "Kidd"), (3171, 4, "Nowitzki"), (3178, 4, "T-Mac"),
    (3183, 4, "Iverson"), (3186, 4, "Pierce"), (3197, 4, "Parker"), (3204, 4, "Carter"),
    (3227, 5, "Allen"), (3233, 5, "Ginobili"), (3230, 5, "Gasol"), (31531, 5, "Stoudemire"),
]

# matchup-table header -> key in the output
COLUMNS = {
    "FGM/A*": "fgma", "FG%": "fg_pct", "FTM/A*": "ftma", "FT%": "ft_pct",
    "3PTM": "threes", "PTS": "pts", "REB": "reb", "AST": "ast",
    "ST": "st", "BLK": "blk", "TO": "to",
}

DELAY = 0.4  # seconds between requests — be gentle with Yahoo

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
})


def fetch(url):
    """GET with a few retries; returns the page HTML."""
    last = None
    for attempt in range(3):
        try:
            time.sleep(DELAY)
            r = session.get(url, timeout=30)
            if r.status_code == 200:
                return r.text
            last = f"HTTP {r.status_code}"
        except requests.RequestException as e:
            last = str(e)
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"{url}: {last}")


def num(text):
    text = text.strip()
    if text in ("", "-"):
        return None
    try:
        return float(text) if "." in text else int(text)
    except ValueError:
        return None


def made_attempts(text):
    m = re.match(r"^\s*(\d+)\s*/\s*(\d+)\s*$", text)
    return (int(m.group(1)), int(m.group(2))) if m else (None, None)


def parse_league_home(html, lid):
    """Team list + current week + current week's matchup pairs from the league home page."""
    soup = BeautifulSoup(html, "lxml")
    teams = {}
    for a in soup.find_all("a", href=True):
        m = re.search(rf"/nba/{lid}/(\d+)$", a["href"])
        name = a.get_text(" ", strip=True)
        if m and name:
            teams.setdefault(m.group(1), name)
    pairs = re.findall(r"matchup\?week=(\d+)&(?:amp;)?mid1=(\d+)&(?:amp;)?mid2=(\d+)", html)
    weeks = collections.Counter(int(w) for w, _, _ in pairs)
    current = weeks.most_common(1)[0][0] if weeks else None
    return teams, current


def parse_matchup(html, lid):
    """Both teams' weekly totals from one matchup page, plus the week's dates."""
    soup = BeautifulSoup(html, "lxml")
    table = None
    for t in soup.find_all("table"):
        heads = [th.get_text(" ", strip=True) for th in t.find_all("th")]
        if "FGM/A*" in heads and "PTS" in heads:
            table = t
            break
    if table is None:
        raise RuntimeError("stats table not found")

    heads = [th.get_text(" ", strip=True) for th in table.find("tr").find_all("th")]
    rows = {}
    for tr in table.find_all("tr")[1:]:
        tds = tr.find_all("td")
        if not tds:
            continue
        tid = None
        for a in tds[0].find_all("a", href=True):
            m = re.search(rf"/nba/{lid}/(\d+)$", a["href"])
            if m:
                tid = m.group(1)
        if not tid:
            continue
        rec = {}
        for i, h in enumerate(heads):
            if i >= len(tds):
                break
            key = COLUMNS.get(h)
            val = tds[i].get_text(" ", strip=True)
            if key == "fgma":
                rec["fgm"], rec["fga"] = made_attempts(val)
            elif key == "ftma":
                rec["ftm"], rec["fta"] = made_attempts(val)
            elif key:
                rec[key] = num(val)
        # last column = categories won in this week's league matchup
        rec["score"] = num(tds[-1].get_text(" ", strip=True))
        rows[tid] = rec

    ids = list(rows)
    if len(ids) == 2:
        rows[ids[0]]["opp"], rows[ids[1]]["opp"] = ids[1], ids[0]

    # games played this week ("Games Played 12/34 ... 15/35") — the cup tiebreaker (fewer games wins).
    # Left figure = first team in the stats table, right figure = second team.
    box = soup.find(id="remaining-games")
    if box:
        counts = re.findall(r"(\d+)\s*/\s*(\d+)", box.get_text(" ", strip=True))
        for tid, (played, total) in zip(ids, counts):
            rows[tid]["gp"], rows[tid]["gp_max"] = int(played), int(total)

    dates = sorted(set(re.findall(r"matchup\?week=\d+&(?:amp;)?date=(\d{4}-\d{2}-\d{2})", html)))
    return rows, (dates[0] if dates else None), (dates[-1] if dates else None)


def scrape_week(lid, week, team_ids):
    """Weekly totals for every team in the league for one week (one page per matchup)."""
    out, start, end = {}, None, None
    for tid in sorted(team_ids, key=int):
        if tid in out:
            continue  # already covered by its opponent's matchup page
        html = fetch(f"{BASE}{lid}/matchup?week={week}&mid1={tid}")
        rows, s, e = parse_matchup(html, lid)
        out.update(rows)
        start, end = start or s, end or e
    return {"start": start, "end": end, "teams": out}


def main():
    data = {"leagues": {}}
    if os.path.exists(OUT_FILE):
        with open(OUT_FILE, encoding="utf-8") as f:
            data = json.load(f)

    ok, failed = 0, []
    for lid, tier, name in LEAGUES:
        key = str(lid)
        prev = data["leagues"].get(key, {})
        try:
            teams, current = parse_league_home(fetch(f"{BASE}{lid}"), lid)
            if not teams:
                raise RuntimeError("no teams found on the league page")
            if not current:
                # league not full yet → Yahoo hasn't built its schedule; keep the team list for now
                data["leagues"][key] = {
                    "name": name, "tier": tier, "current_week": None,
                    "teams": teams, "weeks": prev.get("weeks", {}),
                }
                ok += 1
                print(f"WAIT {name:<11} teams={len(teams):<2} (no schedule yet — league not full?)")
                continue
            weeks = dict(prev.get("weeks", {}))
            # always refresh this week and last week (stat corrections); fill in any missing older weeks
            todo = [w for w in range(1, current + 1)
                    if w >= current - 1 or str(w) not in weeks]
            for w in todo:
                weeks[str(w)] = scrape_week(lid, w, teams.keys())
            data["leagues"][key] = {
                "name": name, "tier": tier, "current_week": current,
                "teams": teams, "weeks": weeks,
            }
            ok += 1
            print(f"OK   {name:<11} teams={len(teams):<2} week={current} weeks_scraped={todo}")
        except Exception as e:  # keep the league's previous data if this run fails
            failed.append(name)
            if prev:
                prev["error"] = str(e)[:200]
                data["leagues"][key] = prev
            print(f"FAIL {name:<11} {e}", file=sys.stderr)

    data["updated"] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    print(f"\nDone: {ok}/{len(LEAGUES)} leagues OK" + (f", failed: {', '.join(failed)}" if failed else ""))
    # fail the run only if nothing worked at all (e.g. Yahoo blocked us)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
