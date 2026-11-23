"""
IFSL data sync — one workflow run.

Why this exists: GitHub fires our scheduled runs rarely (sometimes 8–12 h apart), and a run that
lands while NBA games are live gets no stats (Yahoo hides the matchup stats table during games).
So the runs don't depend on GitHub's timing:

  * fixed MORNING collections at 08:05, 09:05, 10:05 and 11:05 Israel time (after the night's games);
  * a run that starts in the evening (from 17:00) or at night collects (evening only) and then stays on —
    handing over to follow-up runs before GitHub's time limit — through all the morning collections;
  * a run between 11:05 and 17:00 collects once and stops.

So ONE run fired anywhere between 17:00 and 11:00 is enough for the whole next morning.
If games are still live during a collection (stats hidden), it retries every 20 minutes.
Times are Israel local time (Asia/Jerusalem), so daylight-saving changes don't shift them.

A collection = cup_scrape.py + drafts_scrape.py + trades_scrape.py, then commit the data files.
"""
import datetime
import os
import subprocess
import sys
import time
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
IL = ZoneInfo("Asia/Jerusalem")
MAX_SECONDS = int(5.6 * 3600)                    # stay under the job's timeout (6 h) — then a follow-up run continues
PAUSE = 20 * 60                                  # between collections while games are still live
# fixed collections (Israel time): every morning hour after the night's games, plus 13:05 and 16:05 because Yahoo
# sometimes finishes its weekly totals hours later (2026-11-23: Sunday's games were missing from 11:00 until ~15:00)
MORNING = [(8, 5), (9, 5), (10, 5), (11, 5), (13, 5), (16, 5)]
EVENING_HOUR = 17                                # from 17:00 a run stays on until the morning collections are done
LIVE_FLAG = os.path.join(HERE, "live_mode.flag")
CONTINUE_FLAG = os.path.join(HERE, "continue.flag")

start = time.time()


def now_il():
    return datetime.datetime.now(IL)


def sh(cmd):
    print(f"$ {cmd}", flush=True)
    return subprocess.run(cmd, shell=True, cwd=HERE).returncode


def commit():
    sh('git config user.name "github-actions[bot]"')
    sh('git config user.email "41898282+github-actions[bot]@users.noreply.github.com"')
    sh("git fetch -q origin main && git reset -q origin/main")   # build on the newest main
    sh("git add cup-data.json drafts-data.json trades-data.json")
    if subprocess.run("git diff --cached --quiet", shell=True, cwd=HERE).returncode == 0:
        print("No changes", flush=True)
        return
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    sh(f'git commit -q -m "Update data {stamp}"')
    if sh("git push -q") != 0:   # someone pushed meanwhile — rebase on it and try once more
        sh("git pull -q --rebase origin main && git push -q")


def run_pass():
    """One full collection + commit. Returns True if games were live (stats hidden) during it."""
    if os.path.exists(LIVE_FLAG):
        os.remove(LIVE_FLAG)
    print(f"--- collecting ({now_il():%H:%M} Israel)", flush=True)
    sh("python cup_scrape.py")
    sh("python drafts_scrape.py")
    sh("python trades_scrape.py")
    commit()
    return os.path.exists(LIVE_FLAG)


def seconds_left():
    return MAX_SECONDS - (time.time() - start)


def sleep_or_hand_over(seconds, why):
    """Sleep. If that would run past the time limit: sleep what's left of this run, then ask for a
    follow-up run and stop (the follow-up sleeps the rest — never a chain of instant runs)."""
    budget = seconds_left() - 600
    if seconds > budget:
        print(f"{why}: waiting {int(max(budget, 0) // 60)} min, then a follow-up run continues", flush=True)
        time.sleep(max(budget, 0))
        open(CONTINUE_FLAG, "w").close()
        sys.exit(0)
    print(f"{why}: waiting {int(seconds // 60)} min", flush=True)
    time.sleep(seconds)


def collect():
    live = run_pass()
    while live:   # games still on — try again in a bit
        sleep_or_hand_over(PAUSE, "Games still live")
        live = run_pass()


def morning_times(day):
    return [datetime.datetime(day.year, day.month, day.day, h, m, tzinfo=IL) for h, m in MORNING]


def main():
    now = now_il()
    first, last = morning_times(now)[0], morning_times(now)[-1]
    if now.hour >= EVENING_HOUR:
        collect()                                                    # evening: collect now…
        todo = morning_times(now + datetime.timedelta(days=1))       # …and stay on for tomorrow morning
    elif now < first:
        todo = morning_times(now)                                    # night: wait for this morning
    elif now <= last:
        collect()                                                    # during the morning: collect now…
        todo = [t for t in morning_times(now) if t > now_il()]       # …and the rest of this morning
    else:
        collect()                                                    # midday/afternoon: collect once
        todo = []
    for t in todo:
        wait = (t - now_il()).total_seconds()
        if wait > 0:
            sleep_or_hand_over(wait, f"Next collection at {t:%H:%M} Israel")
        collect()
    print("Done.", flush=True)


if __name__ == "__main__":
    main()
