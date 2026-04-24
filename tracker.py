#!/usr/bin/env python3
import requests
import time
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import os

CRICAPI_KEY    = "6da89c9a-acab-4c50-a909-cfbba5636fe4"
CRICAPI_BASE   = "https://api.cricapi.com/v1"
TELEGRAM_TOKEN = "8718609997:AAGlMGxsgZSv0PlPTzqMl_R29NQ-bf3-STI"
CHAT_IDS       = ["5023801264", "1372959952"]
POLL_INTERVAL  = 150   # 2.5 min — keeps usage under 100 calls/day free limit
IPL_CHECK_EVERY = 6    # re-check for IPL every 6 polls (~15 min)
PORT           = int(os.environ.get("PORT", 10000))


# ── Health check server (required by Render) ──────────────────────────────────

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"IPL Tracker is running")
    def log_message(self, *args):
        pass

def start_health_server():
    HTTPServer(("0.0.0.0", PORT), HealthHandler).serve_forever()


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
            print(f"[Telegram error] chat={chat_id}: {e}")
    print(f"[ALERT] {text.splitlines()[0]}")


# ── CricAPI helpers ───────────────────────────────────────────────────────────

def get_current_matches() -> list:
    try:
        r = requests.get(
            f"{CRICAPI_BASE}/currentMatches",
            params={"apikey": CRICAPI_KEY, "offset": "0"},
            timeout=10,
        )
        data = r.json()
        print(f"[API] hits today: {data.get('info', {}).get('hitsToday', '?')} / {data.get('info', {}).get('hitsLimit', '?')}")
        return data.get("data", [])
    except Exception as e:
        print(f"[API error] {e}")
        return []


def find_match(matches: list, skip_ids: set):
    """IPL takes priority; falls back to any live match."""
    fallback = None
    for m in matches:
        mid  = m.get("id", "")
        name = m.get("name", "")
        if mid in skip_ids or not m.get("matchStarted") or m.get("matchEnded"):
            continue
        if "indian premier" in name.lower() or "ipl" in name.lower():
            return mid, name
        if fallback is None:
            fallback = (mid, name)
    return fallback if fallback else (None, None)


def get_scorecard(match_id: str) -> dict:
    try:
        r = requests.get(
            f"{CRICAPI_BASE}/match_scorecard",
            params={"apikey": CRICAPI_KEY, "id": match_id},
            timeout=10,
        )
        return r.json().get("data", {})
    except Exception as e:
        print(f"[Scorecard error] {e}")
        return {}


# ── State extraction ──────────────────────────────────────────────────────────

def extract_batting(data: dict) -> dict:
    batters = {}
    for inning in data.get("scorecard", []):
        for b in inning.get("batting", []):
            name = b.get("batsman", {}).get("name", "")
            if name:
                batters[name] = {
                    "r":     int(b.get("r", 0) or 0),
                    "fours": int(b.get("4s", 0) or 0),
                    "sixes": int(b.get("6s", 0) or 0),
                }
    return batters


def extract_scores(data: dict) -> dict:
    scores = {}
    for s in data.get("score", []):
        inning = s.get("inning", "")
        if inning:
            scores[inning] = {
                "r": int(s.get("r", 0) or 0),
                "w": int(s.get("w", 0) or 0),
            }
    return scores


# ── Event detection ───────────────────────────────────────────────────────────

def check_events(data, match_name, prev_batters, prev_scores,
                 milestones_sent, baseline_only=False):
    curr_batters = extract_batting(data)
    curr_scores  = extract_scores(data)

    if not baseline_only:
        # Wickets
        for inning, curr in curr_scores.items():
            prev_w = prev_scores.get(inning, {}).get("w", 0)
            if curr["w"] > prev_w:
                for _ in range(curr["w"] - prev_w):
                    send_alert(
                        f"🔴 WICKET!\n<b>{match_name}</b>\n"
                        f"{inning}: {curr['r']}/{curr['w']}"
                    )

        # Per-batter events
        for name, curr in curr_batters.items():
            prev = prev_batters.get(name, {"r": 0, "fours": 0, "sixes": 0})

            for _ in range(max(0, curr["sixes"] - prev["sixes"])):
                send_alert(f"💥 SIX!\n<b>{name}</b> hits it out of the park!\nScore: {curr['r']} runs")

            for _ in range(max(0, curr["fours"] - prev["fours"])):
                send_alert(f"🏏 FOUR!\n<b>{name}</b> finds the boundary!\nScore: {curr['r']} runs")

            if prev["r"] < 50 <= curr["r"] and f"{name}_50" not in milestones_sent:
                send_alert(f"🌟 FIFTY!\n<b>{name}</b> reaches 50 runs! ({curr['r']}*)")
                milestones_sent.add(f"{name}_50")

            if prev["r"] < 100 <= curr["r"] and f"{name}_100" not in milestones_sent:
                send_alert(f"💯 CENTURY!\n<b>{name}</b> scores a HUNDRED! ({curr['r']}*)")
                milestones_sent.add(f"{name}_100")

    prev_batters.clear()
    prev_batters.update(curr_batters)
    prev_scores.clear()
    prev_scores.update(curr_scores)


# ── Tracker loop ──────────────────────────────────────────────────────────────

def run_tracker():
    print("🏏 IPL Live Tracker started")
    send_alert("🏏 <b>IPL Live Tracker is now active!</b>\nWatching for 4s, 6s, wickets, and milestones.")

    match_id, match_name = None, None
    prev_batters:    dict = {}
    prev_scores:     dict = {}
    milestones_sent: set  = set()
    ended_ids:       set  = set()
    first_poll       = False
    poll_count       = 0

    while True:
        poll_count += 1

        # Find a match if we don't have one, or periodically check for IPL upgrade
        if not match_id or poll_count % IPL_CHECK_EVERY == 0:
            matches = get_current_matches()
            new_id, new_name = find_match(matches, skip_ids=ended_ids)

            if not match_id:
                if not new_id:
                    print("No live match found. Retrying in 60s...")
                    time.sleep(60)
                    continue
                match_id, match_name = new_id, new_name
                print(f"Found: {match_name}  (id={match_id})")
                send_alert(f"🏏 <b>Match Found!</b>\n{match_name}")
                prev_batters.clear()
                prev_scores.clear()
                milestones_sent.clear()
                first_poll = True

            elif new_id and new_id != match_id:
                # Only switch if new match is IPL and current is not
                curr_is_ipl = "indian premier" in match_name.lower() or "ipl" in match_name.lower()
                new_is_ipl  = "indian premier" in new_name.lower()  or "ipl" in new_name.lower()
                if new_is_ipl and not curr_is_ipl:
                    print(f"IPL detected! Switching → {new_name}")
                    send_alert(f"🏏 <b>Switching to IPL!</b>\n{new_name}")
                    match_id, match_name = new_id, new_name
                    prev_batters.clear()
                    prev_scores.clear()
                    milestones_sent.clear()
                    first_poll = True

        # Fetch scorecard
        data = get_scorecard(match_id)
        if not data:
            time.sleep(POLL_INTERVAL)
            continue

        if data.get("matchEnded"):
            send_alert(f"🏁 <b>Match Ended!</b>\n{match_name}")
            ended_ids.add(match_id)
            match_id, match_name = None, None
            time.sleep(60)
            continue

        check_events(data, match_name, prev_batters, prev_scores,
                     milestones_sent, baseline_only=first_poll)

        if first_poll:
            print("Baseline set — watching for new events...")
            first_poll = False

        time.sleep(POLL_INTERVAL)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    threading.Thread(target=start_health_server, daemon=True).start()
    print(f"Health server running on port {PORT}")
    run_tracker()
