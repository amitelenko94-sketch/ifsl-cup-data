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
DEBUG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "debug")  # pages we couldn't read (uploaded as a run artifact)

# (yahoo league id, tier, name) — tier 1 = ליגת העל ... 5 = ליגה חמישית
LEAGUES = [
    (3109, 1, "Jordan"),
    (3130, 2, "Magic"), (3133, 2, "Bird"),
    (3137, 3, "Kobe"), (3140, 3, "Shaq"), (3149, 3, "Duncan"), (3156, 3, "Garnett"),
    (3159, 4, "Nash"), (3170, 4, "Kidd"), (3171, 4, "Nowitzki"), (3178, 4, "T-Mac"),
    (3183, 4, "Iverson"), (3186, 4, "Pierce"), (3197, 4, "Parker"), (3204, 4, "Carter"),
    (3227, 5, "Allen"), (3233, 5, "Ginobili"), (3230, 5, "Gasol"), (31531, 5, "Stoudemire"),
]

# matchup-table header (whitespace removed) -> key in the output
COLUMNS = {
    "FGM/A*": "fgma", "FG%": "fg_pct", "FTM/A*": "ftma", "FT%": "ft_pct",
    "3PTM": "threes", "PTS": "pts", "REB": "reb", "AST": "ast",
    "ST": "st", "BLK": "blk", "TO": "to",
}

DELAY = 0.4  # seconds between requests — be gentle with Yahoo

# real games played (the cup tiebreaker): Yahoo's own "Games Played" also counts players who sat
# in an active slot while injured (0 minutes). We count from the daily lineup pages instead.
ACTIVE_SLOTS = {"PG", "SG", "G", "SF", "PF", "F", "C", "Util", "UTIL"}
DAY_PAGE_BUDGET = 250      # max daily-lineup pages per run (a full day = 228 pages; the rest waits for the next run)
DAY_PAGE_DELAY = 0.8       # extra pause between daily-lineup pages — too many fast requests get "HTTP 999" from Yahoo
DAY_FINAL_HOUR_UTC = 10    # a US game day is final the next day at 10:00 UTC (13:00 Israel)
SLASH = re.compile(r"^(\d+)/(\d+)$")
PLAYER_LINK = re.compile(r"/nba/players/\d+")

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
})


class RateLimited(RuntimeError):
    pass


def fetch(url):
    """GET with a few retries; returns the page HTML."""
    last = None
    for attempt in range(3):
        try:
            time.sleep(DELAY)
            r = session.get(url, timeout=30)
            if r.status_code == 200:
                return r.text
            if r.status_code == 999:  # Yahoo's "slow down" — don't keep knocking
                raise RateLimited(f"{url}: HTTP 999 (Yahoo rate limit)")
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


def head_text(th):
    # "FGM/A *" and "FGM/A*" are the same column — Yahoo's markup around the header varies
    return re.sub(r"\s+", "", th.get_text(""))


class NoStatsTable(RuntimeError):
    pass


def describe_page(soup, html):
    """One-line summary of a page we couldn't read, for the run log."""
    title = soup.title.get_text(strip=True) if soup.title else None
    tables = [[head_text(th) for th in t.find_all("th")][:8] for t in soup.find_all("table")]
    return f"title={title!r} size={len(html)} tables={tables[:5]}"


def parse_matchup(html, lid):
    """Both teams' weekly totals from one matchup page, plus the week's dates."""
    soup = BeautifulSoup(html, "lxml")
    table = None
    for t in soup.find_all("table"):
        heads = [head_text(th) for th in t.find_all("th")]
        if "FGM/A*" in heads and "PTS" in heads:
            table = t
            break
    if table is None:
        raise NoStatsTable("stats table not found — " + describe_page(soup, html))

    heads = [head_text(th) for th in table.find("tr").find_all("th")]
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


debug_saved = 0


def save_debug(name, html):
    global debug_saved
    if debug_saved >= 3:
        return
    os.makedirs(DEBUG_DIR, exist_ok=True)
    with open(os.path.join(DEBUG_DIR, name), "w", encoding="utf-8") as f:
        f.write(html)
    debug_saved += 1


def read_matchup(lid, week, tid):
    """One matchup page. Yahoo sometimes serves a page without the stats table while games
    are live — wait and try again before giving up."""
    url = f"{BASE}{lid}/matchup?week={week}&mid1={tid}"
    for attempt in range(3):
        html = fetch(url)
        try:
            return parse_matchup(html, lid)
        except NoStatsTable:
            if attempt == 2:
                save_debug(f"{lid}-week{week}-team{tid}.html", html)
                raise
            time.sleep(5 * (attempt + 1))


def scrape_week(lid, week, team_ids, prev_week):
    """Weekly totals for every team in the league for one week (one page per matchup).
    A matchup that can't be read keeps its numbers from the previous run instead of
    failing the whole league; those teams are listed under "stale"."""
    prev_week = prev_week or {}
    out, start, end, stale = {}, prev_week.get("start"), prev_week.get("end"), []
    for tid in sorted(team_ids, key=int):
        if tid in out or tid in stale:
            continue  # already covered by its opponent's matchup page
        try:
            rows, s, e = read_matchup(lid, week, tid)
        except NoStatsTable as err:
            print(f"     week {week} team {tid}: {err}", file=sys.stderr)
            old = prev_week.get("teams", {})
            for t in (tid, (old.get(tid) or {}).get("opp")):
                if t and t in old:
                    out[t] = old[t]
                if t:
                    stale.append(t)
            continue
        out.update(rows)
        start, end = s or start, e or end
    week_data = {"start": start, "end": end, "teams": out}
    if stale:
        week_data["stale"] = stale
    return week_data


def played(cells):
    """Did this lineup row really play that day? (same rule as the BlockBuster board)
    Minutes > 0, or any non-zero stat. Injured-but-in-the-lineup shows MIN 0 and 0/0 → not counted;
    no game / DNP shows "-/-" → not counted. Anchor = the FGM/A cell; MIN sits right before it."""
    j = None
    for i, c in enumerate(cells):
        s = (c or "").strip()
        if SLASH.match(s) or s == "-/-":
            j = i
            break
    if j is None:
        return False
    if j >= 1:
        try:
            if float((cells[j - 1] or "").strip()) > 0:
                return True
        except ValueError:
            pass
    for s in cells[j:]:
        s = (s or "").strip()
        m = SLASH.match(s)
        if m:
            if int(m.group(1)) > 0 or int(m.group(2)) > 0:
                return True
        elif s.isdigit() and int(s) > 0:
            return True
    return False


def games_on_day(lid, tid, week, date):
    """How many players of one team really played on one day (active slots only)."""
    soup = BeautifulSoup(fetch(f"{BASE}{lid}/{tid}/team?date={date}&week={week}"), "lxml")
    # the lineup = the table with the most player rows
    best, best_rows = None, 0
    for t in soup.find_all("table"):
        rows = [tr for tr in t.find_all("tr") if tr.find("a", href=PLAYER_LINK)]
        if len(rows) > best_rows:
            best, best_rows = rows, len(rows)
    if not best:
        raise RuntimeError("lineup table not found")
    n = 0
    for tr in best:
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        if cells and cells[0].strip() in ACTIVE_SLOTS and played(cells):
            n += 1
    return n


def week_days(start, end):
    d, last = datetime.date.fromisoformat(start), datetime.date.fromisoformat(end)
    while d <= last:
        yield d.isoformat()
        d += datetime.timedelta(days=1)


def day_is_final(date, now):
    nxt = datetime.date.fromisoformat(date) + datetime.timedelta(days=1)
    return now >= datetime.datetime(nxt.year, nxt.month, nxt.day, DAY_FINAL_HOUR_UTC, tzinfo=datetime.timezone.utc)


def update_real_games(data):
    """Fill league["daily"][date][team] = players who really played, for finished days of this week
    and last week (each day is read once), then sum them into each week's teams as gp_real."""
    now = datetime.datetime.now(datetime.timezone.utc)
    budget, read, failed = DAY_PAGE_BUDGET, 0, 0
    for lid, _, name in LEAGUES:
        L = data["leagues"].get(str(lid))
        if not L or not L.get("current_week"):
            continue
        daily = L.setdefault("daily", {})
        cw = L["current_week"]
        for w in (cw - 1, cw):
            wk = L["weeks"].get(str(w)) or {}
            if not wk.get("start") or not wk.get("end"):
                continue
            for date in week_days(wk["start"], wk["end"]):
                if not day_is_final(date, now):
                    continue
                got = daily.setdefault(date, {})
                for tid in sorted(L["teams"], key=int):
                    if tid in got or budget <= 0:
                        continue
                    budget -= 1
                    try:
                        time.sleep(DAY_PAGE_DELAY)
                        got[tid] = games_on_day(lid, tid, w, date)
                        read += 1
                    except RateLimited as e:  # Yahoo asked us to slow down: stop this step, continue next run
                        print(f"     games: {e} — stopping, will continue next run", file=sys.stderr)
                        budget = 0
                    except Exception as e:  # try again next run
                        failed += 1
                        print(f"     games {name} team {tid} {date}: {e}", file=sys.stderr)

        # sum the days into every stored week; drop daily detail older than last week
        keep_from = (L["weeks"].get(str(cw - 1)) or L["weeks"].get(str(cw)) or {}).get("start")
        for wk in L["weeks"].values():
            if not wk.get("start") or not wk.get("end"):
                continue
            days = list(week_days(wk["start"], wk["end"]))
            complete = [d for d in days if d in daily and all(t in daily[d] for t in wk["teams"])]
            through = None
            for d in days:  # last day of the unbroken run of complete days from the start of the week
                if d not in complete:
                    break
                through = d
            if not complete and "gp_real_through" in wk:
                continue  # an older week whose daily detail was already dropped — keep its totals
            for tid, t in wk["teams"].items():
                if through:
                    t["gp_real"] = sum(daily[d][tid] for d in days if d <= through)
                else:
                    t.pop("gp_real", None)  # nothing counted yet — the site falls back to Yahoo's counter
            wk["gp_real_through"] = through
        if keep_from:
            for d in [d for d in daily if d < keep_from]:
                del daily[d]
    print(f"Real games: {read} day-pages read, {failed} failed" + (", budget used up (continues next run)" if budget <= 0 else ""))


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
            stale = 0
            for w in todo:
                weeks[str(w)] = scrape_week(lid, w, teams.keys(), weeks.get(str(w)))
                stale += len(weeks[str(w)].get("stale", []))
            data["leagues"][key] = {
                "name": name, "tier": tier, "current_week": current,
                "teams": teams, "weeks": weeks,
                "daily": prev.get("daily", {}),   # real-games count per day, read once per day (see update_real_games)
            }
            ok += 1
            note = f"  ({stale} teams kept from the previous run — Yahoo page unreadable)" if stale else ""
            print(f"OK   {name:<11} teams={len(teams):<2} week={current} weeks_scraped={todo}{note}")
        except Exception as e:  # keep the league's previous data if this run fails
            failed.append(name)
            if prev:
                prev["error"] = str(e)[:200]
                data["leagues"][key] = prev
            print(f"FAIL {name:<11} {e}", file=sys.stderr)

    try:
        update_real_games(data)
    except Exception as e:  # never lose the weekly stats because of the games count
        print(f"Real games step failed: {e}", file=sys.stderr)

    data["updated"] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    print(f"\nDone: {ok}/{len(LEAGUES)} leagues OK" + (f", failed: {', '.join(failed)}" if failed else ""))
    # fail the run only if nothing worked at all (e.g. Yahoo blocked us)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
