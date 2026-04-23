#!/usr/bin/env python3
import requests
import time

RAPIDAPI_KEY   = "9dbddf380emsh7cf8e17e473544bp173af4jsn314853b48f43"
TELEGRAM_TOKEN = "8718609997:AAGlMGxsgZSv0PlPTzqMl_R29NQ-bf3-STI"
CHAT_IDS       = ["5023801264", "1372959952"]
POLL_INTERVAL  = 15

HEADERS = {
    "X-RapidAPI-Key":  RAPIDAPI_KEY,
    "X-RapidAPI-Host": "cricbuzz-cricket.p.rapidapi.com",
}


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


# ── Cricbuzz API helpers ───────────────────────────────────────────────────────

def get_live_match(skip_ids: set):
    """IPL takes priority; falls back to any live match. Skips ended match IDs."""
    try:
        r = requests.get(
            "https://cricbuzz-cricket.p.rapidapi.com/matches/v1/live",
            headers=HEADERS,
            timeout=10,
        )
        fallback = None
        for type_match in r.json().get("typeMatches", []):
            for series in type_match.get("seriesMatches", []):
                wrapper     = series.get("seriesAdWrapper", {})
                series_name = wrapper.get("seriesName", "").lower()
                for match in wrapper.get("matches", []):
                    info = match.get("matchInfo", {})
                    mid  = info.get("matchId")
                    if not mid or str(mid) in skip_ids:
                        continue
                    t1   = info.get("team1", {}).get("teamSName", "T1")
                    t2   = info.get("team2", {}).get("teamSName", "T2")
                    name = f"{t1} vs {t2} — {wrapper.get('seriesName', '')}"
                    if "ipl" in series_name or "indian premier" in series_name:
                        return str(mid), name
                    if fallback is None:
                        fallback = (str(mid), name)
        if fallback:
            return fallback
    except Exception as e:
        print(f"[API error] {e}")
    return None, None


def get_scorecard(match_id: str) -> dict:
    try:
        r = requests.get(
            f"https://cricbuzz-cricket.p.rapidapi.com/mcenter/v1/{match_id}/hscard",
            headers=HEADERS,
            timeout=10,
        )
        return r.json()
    except Exception as e:
        print(f"[Scorecard error] {e}")
        return {}


# ── State extraction ──────────────────────────────────────────────────────────

def extract_batting(data: dict) -> dict:
    batters = {}
    for inning in data.get("scorecard", []):
        for b in inning.get("batsman", []):
            name = b.get("name", "")
            if name:
                batters[name] = {
                    "r":     int(b.get("runs", 0) or 0),
                    "fours": int(b.get("fours", 0) or 0),
                    "sixes": int(b.get("sixes", 0) or 0),
                }
    return batters


def extract_scores(data: dict) -> dict:
    scores = {}
    for inning in data.get("scorecard", []):
        iid = inning.get("inningsid", 0)
        scores[iid] = {
            "r":     int(inning.get("score", 0) or 0),
            "w":     int(inning.get("wickets", 0) or 0),
            "label": inning.get("batteamsname", str(iid)),
        }
    return scores


# ── Event detection ───────────────────────────────────────────────────────────

def check_events(data: dict, match_name: str,
                 prev_batters: dict, prev_scores: dict,
                 milestones_sent: set, baseline_only: bool = False):
    curr_batters = extract_batting(data)
    curr_scores  = extract_scores(data)

    if not baseline_only:
        # Wickets
        for iid, curr in curr_scores.items():
            prev_w = prev_scores.get(iid, {}).get("w", 0)
            if curr["w"] > prev_w:
                for _ in range(curr["w"] - prev_w):
                    send_alert(
                        f"🔴 WICKET!\n<b>{match_name}</b>\n"
                        f"{curr['label']}: {curr['r']}/{curr['w']}"
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

    # Always update state
    prev_batters.clear()
    prev_batters.update(curr_batters)
    prev_scores.clear()
    prev_scores.update(curr_scores)


# ── Main loop ─────────────────────────────────────────────────────────────────

def main():
    print("🏏 IPL Live Tracker started")
    send_alert("🏏 <b>IPL Live Tracker is now active!</b>\nWatching for 4s, 6s, wickets, and milestones.")

    match_id, match_name = None, None
    prev_batters:    dict = {}
    prev_scores:     dict = {}
    milestones_sent: set  = set()
    ended_ids:       set  = set()  # never re-pick a finished match

    while True:
        if not match_id:
            print("Searching for live match (IPL priority)...")
            match_id, match_name = get_live_match(skip_ids=ended_ids)
            if not match_id:
                print("No live match found. Retrying in 60s...")
                time.sleep(60)
                continue
            print(f"Found: {match_name}  (id={match_id})")
            send_alert(f"🏏 <b>Match Found!</b>\n{match_name}")
            prev_batters.clear()
            prev_scores.clear()
            milestones_sent.clear()
            first_poll = True
        else:
            first_poll = False

        # If currently on a non-IPL match, check if IPL has started
        if match_id and "ipl" not in match_name.lower() and "indian premier" not in match_name.lower():
            ipl_id, ipl_name = get_live_match(skip_ids=ended_ids | {mid for mid in [] if "ipl" not in match_name.lower()})
            if ipl_id and ipl_id != match_id and ("ipl" in ipl_name.lower() or "indian premier" in ipl_name.lower()):
                print(f"IPL match detected! Switching from {match_name} → {ipl_name}")
                send_alert(f"🏏 <b>Switching to IPL!</b>\n{ipl_name}")
                match_id, match_name = ipl_id, ipl_name
                prev_batters.clear()
                prev_scores.clear()
                milestones_sent.clear()
                first_poll = True

        data = get_scorecard(match_id)
        if not data:
            time.sleep(POLL_INTERVAL)
            continue

        if data.get("ismatchcomplete"):
            send_alert(f"🏁 <b>Match Ended!</b>\n{match_name}")
            ended_ids.add(match_id)
            match_id, match_name = None, None
            time.sleep(60)
            continue

        # first_poll=True: set baseline silently, no alerts for old events
        check_events(data, match_name, prev_batters, prev_scores,
                     milestones_sent, baseline_only=first_poll)

        if first_poll:
            print("Baseline set — watching for new events...")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
