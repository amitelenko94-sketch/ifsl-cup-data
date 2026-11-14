"""
IFSL trades — collector.

Reads the PUBLIC Yahoo trades page of every IFSL league (transactions?transactionsfilter=trade —
trades only: no waiver pickups, drops or free-agent adds) and saves each league's trades to
trades-data.json. The website's stats page reads that file.

A trade is stored once, with both teams in it; the site counts it as one trade for the league
and one trade for each team that took part.

Incremental: Yahoo lists the newest trades first, 25 per page. Each run reads pages until it
reaches a trade it already has, so a normal run is one page per league.

Runs in GitHub Actions next to cup_scrape.py, or locally:
    python trades_scrape.py
"""
import datetime
import json
import os
import re
import sys
import time

import requests
from bs4 import BeautifulSoup

BASE = "https://basketball.fantasysports.yahoo.com/nba/"
OUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trades-data.json")

# (yahoo league id, tier, name) — same list as cup_scrape.py (tier "commish" = the Commissioners league)
LEAGUES = [
    (3109, 1, "Jordan"), (234, "commish", "Commish League"),
    (3130, 2, "Magic"), (3133, 2, "Bird"),
    (3137, 3, "Kobe"), (3140, 3, "Shaq"), (3149, 3, "Duncan"), (3156, 3, "Garnett"),
    (3159, 4, "Nash"), (3170, 4, "Kidd"), (3171, 4, "Nowitzki"), (3178, 4, "T-Mac"),
    (3183, 4, "Iverson"), (3186, 4, "Pierce"), (3197, 4, "Parker"), (3204, 4, "Carter"),
    (3227, 5, "Allen"), (3233, 5, "Ginobili"), (3230, 5, "Gasol"), (31531, 5, "Stoudemire"),
]

DELAY = 0.6
MAX_PAGES = 20          # safety net: 20 pages × 25 = 500 trades per league
PER_PAGE = 25
MONTHS = {m: i for i, m in enumerate(["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}
TS_RE = re.compile(r"([A-Z][a-z]{2})\s+(\d+),\s+(\d+):(\d+)\s*([ap])m")

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
})


class RateLimited(RuntimeError):
    pass


def fetch(url):
    last = None
    for attempt in range(3):
        try:
            time.sleep(DELAY)
            r = session.get(url, timeout=30)
            if r.status_code == 200:
                return r.text
            if r.status_code == 999:  # Yahoo's "slow down"
                raise RateLimited(f"{url}: HTTP 999")
            last = f"HTTP {r.status_code}"
        except requests.RequestException as e:
            last = str(e)
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"{url}: {last}")


def season_year_for(month, now):
    """The season runs Oct–Jun: Aug–Dec dates belong to the season's first year, Jan–Jul to the next."""
    start = now.year if now.month >= 8 else now.year - 1
    return start if month >= 8 else start + 1


def parse_when(text, now):
    m = TS_RE.search(text or "")
    if not m:
        return None
    mon, day, hr, mn, ap = MONTHS[m.group(1)], int(m.group(2)), int(m.group(3)), int(m.group(4)), m.group(5)
    hr = hr % 12 + (12 if ap == "p" else 0)
    try:
        return datetime.datetime(season_year_for(mon, now), mon, day, hr, mn).strftime("%Y-%m-%dT%H:%M")
    except ValueError:
        return None


def parse_page(html, lid, now):
    """Trades on one page, newest first: [{when, sides:[{team, name, players:[...]}, …]}].
    Every row = one team and the players it RECEIVED; a trade's first row has an extra icon cell."""
    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table")
    if table is None:
        return []
    trades, cur = [], None
    for tr in table.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        players = []
        for a in tr.find_all("a", href=re.compile(r"/nba/players/\d+$")):
            name = a.get_text(strip=True)
            if name and name not in players:
                players.append(name)
        team, team_name = None, None
        for a in tr.find_all("a", href=re.compile(rf"/nba/{lid}/\d+$")):
            team = re.search(rf"/nba/{lid}/(\d+)$", a["href"]).group(1)
            team_name = a.get_text(strip=True) or team_name
        if not team or not players:
            continue
        when = parse_when(tr.get_text(" ", strip=True), now)
        starts_trade = len(cells) >= 4 or cur is None   # the icon cell (rowspan) marks the first row of a trade
        if starts_trade:
            cur = {"when": when, "sides": []}
            trades.append(cur)
        cur["when"] = cur["when"] or when
        cur["sides"].append({"team": team, "name": team_name, "players": players})
    # a trade needs two teams; drop anything malformed
    return [t for t in trades if len({s["team"] for s in t["sides"]}) >= 2]


def signature(trade):
    return "~".join(sorted(f"{s['team']}:{'|'.join(sorted(s['players']))}" for s in trade["sides"])) + "@" + (trade["when"] or "")


def scrape_league(lid, known):
    """New trades of one league (newest first), stopping at the first trade we already have."""
    now = datetime.datetime.now()
    new = []
    for page in range(MAX_PAGES):
        url = f"{BASE}{lid}/transactions?transactionsfilter=trade" + (f"&count={page * PER_PAGE}" if page else "")
        trades = parse_page(fetch(url), lid, now)
        reached_known = False
        for t in trades:
            if signature(t) in known:
                reached_known = True
                break
            new.append(t)
        if reached_known or len(trades) < PER_PAGE:
            break
    return new


def main():
    data = {"leagues": {}}
    if os.path.exists(OUT_FILE):
        with open(OUT_FILE, encoding="utf-8") as f:
            data = json.load(f)

    added, failed = 0, []
    for lid, tier, name in LEAGUES:
        key = str(lid)
        prev = data["leagues"].get(key, {})
        old = prev.get("trades", [])
        try:
            new = scrape_league(lid, {signature(t) for t in old})
            data["leagues"][key] = {"name": name, "tier": tier, "trades": new + old}
            added += len(new)
            print(f"OK   {name:<15} +{len(new):<3} total {len(new) + len(old)}")
        except RateLimited as e:
            print(f"STOP Yahoo rate limit — keeping the rest for the next run ({e})", file=sys.stderr)
            break
        except Exception as e:  # keep this league's previous trades
            failed.append(name)
            if prev:
                data["leagues"][key] = prev
            print(f"FAIL {name:<15} {e}", file=sys.stderr)

    data["updated"] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    print(f"\nDone: {added} new trades" + (f", failed: {', '.join(failed)}" if failed else ""))
    sys.exit(0)  # never fail the workflow over the trades — the next run tries again


if __name__ == "__main__":
    main()
