#!/usr/bin/env python3
"""
IPL Live Tracker — no API key required.
Scrapes live match data directly from Cricbuzz pages.
Sends Telegram alerts when any batter reaches 50 or 100.
"""
import re
import time
import random
import threading
import requests
from http.server import HTTPServer, BaseHTTPRequestHandler
import os

TELEGRAM_TOKEN  = "8718609997:AAGlMGxsgZSv0PlPTzqMl_R29NQ-bf3-STI"
CHAT_IDS        = ["5023801264", "1372959952"]
POLL_INTERVAL   = 15    # poll every 15s — alert fires within 15s of milestone
NO_MATCH_SLEEP  = 600   # 10 min when no live match found
PORT            = int(os.environ.get("PORT", 10000))

BASE = "https://www.cricbuzz.com"
HDR  = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.cricbuzz.com/",
}


# ── Health check server (required by Render) ──────────────────────────────────

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"IPL Tracker running")
    def log_message(self, *args):
        pass

def start_health_server():
    HTTPServer(("0.0.0.0", PORT), HealthHandler).serve_forever()

def self_ping():
    """Hit our own health endpoint every 5 min to prevent Render free-plan sleep."""
    time.sleep(60)  # wait for server to start
    while True:
        try:
            requests.get(f"http://localhost:{PORT}", timeout=5)
        except Exception:
            pass
        time.sleep(300)


# ── Telegram ──────────────────────────────────────────────────────────────────

def send_alert(text: str):
    for chat_id in CHAT_IDS:
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                data={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
                timeout=10,
            )
        except Exception as e:
            print(f"[Telegram error] {e}")
    print(f"[ALERT] {text.splitlines()[0]}")


# ── Cricbuzz scraper ──────────────────────────────────────────────────────────

def _fetch_rsc(url: str) -> str:
    """Fetch a Cricbuzz page and return the combined React Server Components data."""
    try:
        r = requests.get(url, headers=HDR, timeout=15)
        chunks = re.findall(r'self\.__next_f\.push\(\[1,"(.+?)"\]\)', r.text, re.DOTALL)
        combined = "".join(chunks)
        try:
            combined = combined.encode().decode("unicode_escape")
        except Exception:
            pass
        return combined
    except Exception as e:
        print(f"[Fetch error] {url}: {e}")
        return ""


DEAD_STATES = {"complete", "preview", "upcoming", ""}

def get_live_match() -> tuple:
    """
    Scrape the Cricbuzz live-scores page.
    IPL takes priority; falls back to any other live match.
    Returns (match_id, match_name) or (None, None).
    """
    data = _fetch_rsc(f"{BASE}/cricket-match/live")
    if not data:
        return None, None

    match_ids = list(dict.fromkeys(re.findall(r'"matchId":(\d+)', data)))

    ipl_match = None
    fallback   = None

    for mid in match_ids:
        idx = data.find(f'"matchId":{mid}')
        ctx = data[max(0, idx - 400): idx + 700]

        state_val = re.search(r'"state":"([^"]+)"', ctx)
        state_str = state_val.group(1) if state_val else ""

        if state_str.lower() in DEAD_STATES:
            continue

        t1     = re.search(r'"team1".*?"teamSName":"([^"]+)"', ctx)
        t2     = re.search(r'"team2".*?"teamSName":"([^"]+)"', ctx)
        series = re.search(r'"seriesName":"([^"]+)"', ctx)
        t1n    = t1.group(1) if t1 else "?"
        t2n    = t2.group(1) if t2 else "?"
        sn     = series.group(1) if series else "?"
        name   = f"{t1n} vs {t2n} — {sn}"

        is_ipl = "indian premier" in ctx.lower() or "ipl" in sn.lower()
        if is_ipl:
            ipl_match = (mid, name)
            break
        if fallback is None:
            fallback = (mid, name)

    result = ipl_match or fallback
    if result:
        print(f"[Live] matchId={result[0]}: {result[1]}")
    return result if result else (None, None)


def get_batting_scores(match_id: str) -> dict:
    """
    Scrape the scorecard page and return {player_name: runs} for all batters.
    """
    data = _fetch_rsc(f"{BASE}/live-cricket-scorecard/{match_id}")
    if not data:
        return {}

    batters = {}
    # Each batter object looks like:
    # "bat_1":{"batId":...,"batName":"Virat Kohli",...,"runs":72,...}
    for m in re.finditer(
        r'"bat_\d+":\{[^}]*"batName":"([^"]+)"[^}]*"runs":(\d+)', data
    ):
        name = m.group(1)
        runs = int(m.group(2))
        batters[name] = {"r": runs}

    return batters


def is_match_complete(match_id: str) -> bool:
    """Check if the match has ended by re-reading the live page."""
    data = _fetch_rsc(f"{BASE}/cricket-match/live")
    idx = data.find(f'"matchId":{match_id}')
    if idx == -1:
        return True   # no longer in live list → treat as ended
    ctx = data[max(0, idx - 100): idx + 300]
    state = re.search(r'"state":"([^"]+)"', ctx)
    return state.group(1).lower() in ("complete",) if state else False


# ── Event detection ───────────────────────────────────────────────────────────

def check_milestones(match_id, match_name, prev_batters, milestones_sent,
                     baseline_only=False):
    curr_batters = get_batting_scores(match_id)
    if not curr_batters:
        return

    if not baseline_only:
        for name, curr in curr_batters.items():
            prev_r = prev_batters.get(name, {}).get("r", 0)

            if prev_r < 50 <= curr["r"] and f"{name}_50" not in milestones_sent:
                send_alert(
                    f"🌟 FIFTY!\n<b>{name}</b> reaches 50 runs! ({curr['r']}*)\n"
                    f"<b>{match_name}</b>"
                )
                milestones_sent.add(f"{name}_50")

            if prev_r < 100 <= curr["r"] and f"{name}_100" not in milestones_sent:
                send_alert(
                    f"💯 CENTURY!\n<b>{name}</b> scores a HUNDRED! ({curr['r']}*)\n"
                    f"<b>{match_name}</b>"
                )
                milestones_sent.add(f"{name}_100")

    prev_batters.clear()
    prev_batters.update(curr_batters)


# ── Tracker loop ──────────────────────────────────────────────────────────────

def run_tracker():
    print("🏏 IPL Milestone Tracker started (no API key)")
    send_alert("🏏 <b>IPL Milestone Tracker active!</b>\nAlerts for 50s and 100s only.")

    match_id, match_name = None, None
    prev_batters:    dict = {}
    milestones_sent: set  = set()
    first_poll            = False

    while True:
        # Find live IPL match
        if not match_id:
            new_id, new_name = get_live_match()
            if not new_id:
                print(f"No live IPL match. Checking again in {NO_MATCH_SLEEP // 60} min...")
                time.sleep(NO_MATCH_SLEEP)
                continue
            match_id, match_name = new_id, new_name
            print(f"Found: {match_name}  (id={match_id})")
            send_alert(f"🏏 <b>IPL Match Started!</b>\n{match_name}")
            prev_batters.clear()
            milestones_sent.clear()
            first_poll = True

        # Check if match ended
        if is_match_complete(match_id):
            send_alert(f"🏁 <b>Match Ended!</b>\n{match_name}")
            match_id, match_name = None, None
            time.sleep(NO_MATCH_SLEEP)
            continue

        # Poll milestones
        check_milestones(match_id, match_name, prev_batters, milestones_sent,
                         baseline_only=first_poll)
        if first_poll:
            print("Baseline set — watching for 50s and 100s...")
            first_poll = False

        time.sleep(POLL_INTERVAL + random.uniform(-3, 3))


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    threading.Thread(target=start_health_server, daemon=True).start()
    threading.Thread(target=self_ping, daemon=True).start()
    print(f"Health server on port {PORT}")
    run_tracker()
