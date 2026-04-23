#!/usr/bin/env python3
import requests

RAPIDAPI_KEY   = "9dbddf380emsh7cf8e17e473544bp173af4jsn314853b48f43"
TELEGRAM_TOKEN = "8718609997:AAGlMGxsgZSv0PlPTzqMl_R29NQ-bf3-STI"
CHAT_ID        = "5023801264"

HEADERS = {
    "X-RapidAPI-Key":  RAPIDAPI_KEY,
    "X-RapidAPI-Host": "cricbuzz-cricket.p.rapidapi.com",
}

def send(text):
    r = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        data={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
        timeout=10,
    )
    print("Telegram:", "OK" if r.ok else r.text)

# ── Step 1: Live matches ──────────────────────────────────────────────────────
print("\n[1] Fetching live matches...")
try:
    r = requests.get(
        "https://cricbuzz-cricket.p.rapidapi.com/matches/v1/live",
        headers=HEADERS, timeout=10,
    )
    data = r.json()
    print("    ✅ API connected")
except Exception as e:
    print(f"    ❌ {e}")
    send("❌ <b>Test Failed</b> — Could not reach Cricbuzz API.")
    exit(1)

# Pick any live match
match_id = match_name = None
for tm in data.get("typeMatches", []):
    for s in tm.get("seriesMatches", []):
        w = s.get("seriesAdWrapper", {})
        for m in w.get("matches", []):
            info = m.get("matchInfo", {})
            mid  = info.get("matchId")
            if mid:
                t1 = info.get("team1", {}).get("teamSName", "T1")
                t2 = info.get("team2", {}).get("teamSName", "T2")
                match_id   = str(mid)
                match_name = f"{t1} vs {t2} — {w.get('seriesName','')}"
                break
        if match_id:
            break
    if match_id:
        break

if not match_id:
    print("    ⚠️  No live matches right now")
    send("⚠️ <b>Tracker Test</b>\nAPI ✅  No live matches right now.\nTracker will auto-detect IPL matches when they go live.")
    exit(0)

print(f"    Using: {match_name} (id={match_id})")

# ── Step 2: Scorecard ─────────────────────────────────────────────────────────
print("\n[2] Fetching scorecard...")
try:
    r = requests.get(
        f"https://cricbuzz-cricket.p.rapidapi.com/mcenter/v1/{match_id}/hscard",
        headers=HEADERS, timeout=10,
    )
    sc = r.json()
    innings = sc.get("scorecard", [])
    print(f"    ✅ Scorecard OK — {len(innings)} inning(s)")
except Exception as e:
    print(f"    ❌ {e}")
    send("❌ <b>Test Failed</b> — Could not fetch scorecard.")
    exit(1)

# Build score summary
score_lines = "\n".join(
    f"  {inn.get('batteamsname','?')}: {inn.get('score','?')}/{inn.get('wickets','?')} ({inn.get('overs','?')} ov)"
    for inn in innings
) or "  Score not available"

# ── Step 3: Telegram ──────────────────────────────────────────────────────────
print("\n[3] Sending Telegram alert...")
send(
    f"✅ <b>Tracker Test — All Systems OK!</b>\n\n"
    f"<b>Live match used for test:</b>\n{match_name}\n\n"
    f"<b>Current Score:</b>\n{score_lines}\n\n"
    f"API ✅  Scorecard ✅  Telegram ✅\n"
    f"Your IPL tracker is ready to go! 🏏"
)
print("\n✅ All checks passed — tracker is ready for IPL!")
