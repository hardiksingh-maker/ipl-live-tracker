#!/usr/bin/env python3
import requests
import time
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import os

RAPIDAPI_KEY   = "9dbddf380emsh7cf8e17e473544bp173af4jsn314853b48f43"
CRICBUZZ_HOST  = "cricbuzz-cricket.p.rapidapi.com"
CRICBUZZ_BASE  = f"https://{CRICBUZZ_HOST}"
TELEGRAM_TOKEN = "8718609997:AAGlMGxsgZSv0PlPTzqMl_R29NQ-bf3-STI"
CHAT_IDS       = ["5023801264", "1372959952"]
POLL_INTERVAL  = 60     # 1 min during live match — Cricbuzz allows more calls
NO_MATCH_SLEEP = 1800   # 30 min when no live match
IPL_CHECK_EVERY = 10    # re-check for IPL upgrade every 10 polls (~10 min)
PORT           = int(os.environ.get("PORT", 10000))

HEADERS = {
    "X-RapidAPI-Key":  RAPIDAPI_KEY,
    "X-RapidAPI-Host": CRICBUZZ_HOST,
}


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


# ── Cricbuzz helpers ──────────────────────────────────────────────────────────

def get_live_matches() -> list:
    """Return list of live match dicts from Cricbuzz."""
    try:
        r = requests.get(f"{CRICBUZZ_BASE}/matches/v1/live", headers=HEADERS, timeout=10)
        data = r.json()
        matches = []
        for tm in data.get("typeMatches", []):
            for sm in tm.get("seriesMatches", []):
                wrapper = sm.get("seriesAdWrapper", {})
                for m in wrapper.get("matches", []):
                    info = m.get("matchInfo", {})
                    score = m.get("matchScore", {})
                    matches.append({"info": info, "score": score, "series": wrapper.get("seriesName", "")})
        print(f"[API] live matches: {len(matches)}")
        return matches
    except Exception as e:
        print(f"[API error] {e}")
        return []


def find_match(matches: list, skip_ids: set):
    """IPL takes priority; falls back to first live match."""
    fallback = None
    for m in matches:
        info = m["info"]
        mid  = str(info.get("matchId", ""))
        series = m["series"]
        t1   = info.get("team1", {}).get("teamSName", "?")
        t2   = info.get("team2", {}).get("teamSName", "?")
        name = f"{t1} vs {t2} — {series}"
        if mid in skip_ids:
            continue
        state = info.get("state", "").lower()
        if state in ("complete", "preview", ""):
            continue
        if "indian premier" in series.lower() or "ipl" in series.lower():
            return mid, name
        if fallback is None:
            fallback = (mid, name)
    return fallback if fallback else (None, None)


def get_scorecard(match_id: str) -> dict:
    try:
        r = requests.get(
            f"{CRICBUZZ_BASE}/mcenter/v1/{match_id}/hscard",
            headers=HEADERS, timeout=10,
        )
        return r.json()
    except Exception as e:
        print(f"[Scorecard error] {e}")
        return {}


# ── State extraction ──────────────────────────────────────────────────────────

def extract_batting(sc: dict) -> dict:
    """Returns {name: {r, fours, sixes}} for all batters in all innings."""
    batters = {}
    for inning in sc.get("scorecard", []):
        for b in inning.get("batTeamDetails", {}).get("batsmenData", {}).values():
            name = b.get("batName", "")
            if name:
                batters[name] = {
                    "r":     int(b.get("runs", 0) or 0),
                    "fours": int(b.get("fours", 0) or 0),
                    "sixes": int(b.get("sixes", 0) or 0),
                }
    return batters


def extract_scores(sc: dict) -> dict:
    """Returns {inning_label: {r, w}} from scorecard innings headers."""
    scores = {}
    for inning in sc.get("scorecard", []):
        label = inning.get("batTeamDetails", {}).get("batTeamName", "")
        score_details = inning.get("scoreDetails", {})
        if label:
            scores[label] = {
                "r": int(score_details.get("runs", 0) or 0),
                "w": int(score_details.get("wickets", 0) or 0),
            }
    return scores


def is_match_complete(sc: dict) -> bool:
    header = sc.get("matchHeader", {})
    return header.get("complete", False) or header.get("state", "") == "complete"


# ── Event detection ───────────────────────────────────────────────────────────

def check_events(sc, match_name, prev_batters, prev_scores,
                 milestones_sent, baseline_only=False):
    curr_batters = extract_batting(sc)
    curr_scores  = extract_scores(sc)

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
    print("🏏 IPL Live Tracker started (Cricbuzz API)")
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

        # Find / upgrade match
        if not match_id or poll_count % IPL_CHECK_EVERY == 0:
            live = get_live_matches()
            new_id, new_name = find_match(live, skip_ids=ended_ids)

            if not match_id:
                if not new_id:
                    print(f"No live match. Retrying in {NO_MATCH_SLEEP//60} min...")
                    time.sleep(NO_MATCH_SLEEP)
                    continue
                match_id, match_name = new_id, new_name
                print(f"Found: {match_name}  (id={match_id})")
                send_alert(f"🏏 <b>Match Found!</b>\n{match_name}")
                prev_batters.clear(); prev_scores.clear(); milestones_sent.clear()
                first_poll = True

            elif new_id and new_id != match_id:
                curr_is_ipl = "indian premier" in match_name.lower() or "ipl" in match_name.lower()
                new_is_ipl  = "indian premier" in new_name.lower()  or "ipl" in new_name.lower()
                if new_is_ipl and not curr_is_ipl:
                    print(f"IPL detected! Switching → {new_name}")
                    send_alert(f"🏏 <b>Switching to IPL!</b>\n{new_name}")
                    match_id, match_name = new_id, new_name
                    prev_batters.clear(); prev_scores.clear(); milestones_sent.clear()
                    first_poll = True

        # Fetch scorecard
        sc = get_scorecard(match_id)
        if not sc:
            time.sleep(POLL_INTERVAL)
            continue

        if is_match_complete(sc):
            send_alert(f"🏁 <b>Match Ended!</b>\n{match_name}")
            ended_ids.add(match_id)
            match_id, match_name = None, None
            time.sleep(NO_MATCH_SLEEP)
            continue

        check_events(sc, match_name, prev_batters, prev_scores,
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
