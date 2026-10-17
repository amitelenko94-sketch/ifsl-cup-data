"""
IFSL drafts — collector.

Reads the PUBLIC Yahoo "Draft Results" page of every IFSL league (19 leagues + the
Commissioners league) and saves each league's auction picks to drafts-data.json.
The website's drafts page reads that file, so new drafts show up on their own.

Runs in GitHub Actions next to cup_scrape.py, or locally:
    python drafts_scrape.py
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
OUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "drafts-data.json")

# (yahoo league id, tier, name) — tier 1..5, "commish" = the Commissioners league
LEAGUES = [
    (3109, 1, "Jordan"), (234, "commish", "Commish League"),
    (3130, 2, "Magic"), (3133, 2, "Bird"),
    (3137, 3, "Kobe"), (3140, 3, "Shaq"), (3149, 3, "Duncan"), (3156, 3, "Garnett"),
    (3159, 4, "Nash"), (3170, 4, "Kidd"), (3171, 4, "Nowitzki"), (3178, 4, "T-Mac"),
    (3183, 4, "Iverson"), (3186, 4, "Pierce"), (3197, 4, "Parker"), (3204, 4, "Carter"),
    (3227, 5, "Allen"), (3233, 5, "Ginobili"), (3230, 5, "Gasol"), (31531, 5, "Stoudemire"),
]

DELAY = 0.4
session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
})


def fetch(url):
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


def clean(text):
    # Yahoo sometimes uses non-breaking spaces (e.g. before "(DEN - C)")
    return re.sub(r"\s+", " ", text.replace(" ", " ")).strip()


def parse_draft(html):
    """Rows [pick, "Player (NBA - POS)", price, "fantasy team"] — empty list if the league hasn't drafted."""
    soup = BeautifulSoup(html, "lxml")
    for table in soup.find_all("table"):
        heads = [clean(th.get_text(" ")) for th in table.find_all("th")]
        if heads[:4] != ["Pick", "Player", "Salary", "Team"]:
            continue
        picks = []
        for tr in table.find_all("tr")[1:]:
            tds = [clean(td.get_text(" ")) for td in tr.find_all("td")]
            if len(tds) < 4:
                continue
            m_pick, m_price = re.match(r"(\d+)", tds[0]), re.search(r"(\d+)", tds[2])
            if not m_pick or not tds[1] or not tds[3]:
                continue
            picks.append([int(m_pick.group(1)), tds[1], int(m_price.group(1)) if m_price else 0, tds[3]])
        return picks
    return []


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
            picks = parse_draft(fetch(f"{BASE}{lid}/draftresults"))
            data["leagues"][key] = {"name": name, "tier": tier, "picks": picks}
            ok += 1
            print(f"OK   {name:<15} picks={len(picks)}")
        except Exception as e:  # keep the previous data for this league if this run fails
            failed.append(name)
            if prev:
                prev["error"] = str(e)[:200]
                data["leagues"][key] = prev
            print(f"FAIL {name:<15} {e}", file=sys.stderr)

    data["updated"] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    print(f"\nDone: {ok}/{len(LEAGUES)} leagues OK" + (f", failed: {', '.join(failed)}" if failed else ""))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
