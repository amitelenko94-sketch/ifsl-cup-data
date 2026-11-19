"""
IFSL data sync — one workflow run.

Why this exists: GitHub fires our scheduled runs rarely (sometimes 8–12 h apart), and a run that
lands while NBA games are live gets no stats (Yahoo hides the matchup stats table during games).
So a run that starts at night doesn't give up: it waits until the night's games are over and the
day is final (06:05 UTC = 08:05 Israel), then collects. If games are still live it tries again every
20 minutes. If the run would hit GitHub's time limit first, it leaves a "continue.flag" and the
workflow starts a follow-up run that carries on.

A pass = cup_scrape.py + drafts_scrape.py + trades_scrape.py, then commit the data files.
"""
import datetime
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
MAX_SECONDS = int(5.6 * 3600)  # stay under the job's timeout (6 h) — then a follow-up run continues
PAUSE = 20 * 60                # between passes while games are still live
NIGHT_START_UTC = 23           # 01:00 Israel (winter) — NBA games run from about now…
DAY_FINAL_UTC = (6, 5)         # …and the night is final at 06:00 UTC (08:00 Israel); collect a bit after
LIVE_FLAG = os.path.join(HERE, "live_mode.flag")
CONTINUE_FLAG = os.path.join(HERE, "continue.flag")

start = time.time()


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


def main():
    now = datetime.datetime.now(datetime.timezone.utc)
    if now.hour >= NIGHT_START_UTC or now.hour < DAY_FINAL_UTC[0]:
        # night: games are live or still to come — collect once they're over and the day is final
        target = now.replace(hour=DAY_FINAL_UTC[0], minute=DAY_FINAL_UTC[1], second=0, microsecond=0)
        if target <= now:
            target += datetime.timedelta(days=1)
        sleep_or_hand_over((target - now).total_seconds(), "Night games")
    live = run_pass()
    while live:   # late games still on — try again in a bit
        sleep_or_hand_over(PAUSE, "Games still live")
        live = run_pass()
    print("Data complete.", flush=True)


if __name__ == "__main__":
    main()
