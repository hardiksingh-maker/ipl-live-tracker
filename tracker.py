#!/usr/bin/env python3
"""
IPL Live Tracker — no API key required.
Scrapes Cricbuzz directly. Alerts for powerplay score, 50s and 100s.
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
POLL_INTERVAL   = 15            # seconds — alert fires within 15s of milestone
NO_MATCH_SLEEP  = 600           # 10 min when no live match
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


# ── Health check server ───────────────────────────────────────────────────────

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
    time.sleep(60)
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
    """IPL priority, fallback to any live match. Returns (match_id, match_name)."""
    data = _fetch_rsc(f"{BASE}/cricket-match/live")
    if not data:
        return None, None

    match_ids = list(dict.fromkeys(re.findall(r'"matchId":(\d+)', data)))
    ipl_match = fallback = None

    for mid in match_ids:
        idx = data.find(f'"matchId":{mid}')
        ctx = data[max(0, idx - 400): idx + 700]
        sv  = re.search(r'"state":"([^"]+)"', ctx)
        st  = sv.group(1) if sv else ""
        if st.lower() in DEAD_STATES:
            continue
        t1  = re.search(r'"team1".*?"teamSName":"([^"]+)"', ctx)
        t2  = re.search(r'"team2".*?"teamSName":"([^"]+)"', ctx)
        sn  = re.search(r'"seriesName":"([^"]+)"', ctx)
        t1n = t1.group(1) if t1 else "?"
        t2n = t2.group(1) if t2 else "?"
        name = f"{t1n} vs {t2n} — {sn.group(1) if sn else '?'}"
        if "indian premier" in ctx.lower() or (sn and "ipl" in sn.group(1).lower()):
            ipl_match = (mid, name); break
        if not fallback:
            fallback = (mid, name)

    result = ipl_match or fallback
    if result:
        print(f"[Live] matchId={result[0]}: {result[1]}")
    return result or (None, None)


def get_scorecard(match_id: str) -> dict:
    data = _fetch_rsc(f"{BASE}/live-cricket-scorecard/{match_id}")
    if not data:
        return {}
    batters = {}
    for m in re.finditer(r'"bat_\d+":\{[^}]*"batName":"([^"]+)"[^}]*"runs":(\d+)', data):
        name, runs = m.group(1), int(m.group(2))
        if name and runs > batters.get(name, -1):  # keep highest run count if name appears twice
            batters[name] = runs
    if not batters:
        return {}
    return {"batters": batters}


def is_match_complete(match_id: str) -> bool:
    data = _fetch_rsc(f"{BASE}/cricket-match/live")
    idx  = data.find(f'"matchId":{match_id}')
    if idx == -1:
        return True
    ctx   = data[max(0, idx - 100): idx + 300]
    state = re.search(r'"state":"([^"]+)"', ctx)
    return state.group(1).lower() == "complete" if state else False


# ── Alert messages ────────────────────────────────────────────────────────────

def _coupon(name: str, milestone: int) -> str:
    last = name.strip().split()[-1].upper()
    return f"{last}{milestone}"


def fifty_msg(player: str, runs: int) -> str:
    code = _coupon(player, 50)
    return (
        f"🌟 𝗙𝗶𝗳𝘁𝘆 𝗯𝘆 {player} 🚀\n"
        f"Big moves need perfect timing — just like trading 👀 📊 "
        f"Unlock Univest Pro Access Now ⚡ 6+9 Months FREE | Code: {code} ⏳ Only 15 mins left!"
    )

def century_msg(player: str, runs: int) -> str:
    code = _coupon(player, 100)
    return (
        f"💯 𝗖𝗲𝗻𝘁𝘂𝗿𝘆 𝗯𝘆 {player} 🚀\n"
        f"Big moves need perfect timing — just like trading 👀 📊 "
        f"Unlock Univest Pro Access Now ⚡ 6+9 Months FREE | Code: {code} ⏳ Only 15 mins left!"
    )


# ── Event detection ───────────────────────────────────────────────────────────

def check_events(match_id, prev_batters, milestones_sent, baseline_only=False):
    sc = get_scorecard(match_id)
    if not sc:
        return  # keep prev_batters intact — don't reset on a bad fetch

    curr_batters = sc.get("batters", {})
    if not curr_batters:
        return

    if not baseline_only:
        # ── milestone alerts ──
        for name, runs in curr_batters.items():
            prev_r = prev_batters.get(name, 0)

            if prev_r < 50 <= runs and f"{name}_50" not in milestones_sent:
                send_alert(fifty_msg(name, runs))
                milestones_sent.add(f"{name}_50")

            if prev_r < 100 <= runs and f"{name}_100" not in milestones_sent:
                send_alert(century_msg(name, runs))
                milestones_sent.add(f"{name}_100")

    prev_batters.clear()
    prev_batters.update(curr_batters)


# ── Tracker loop ──────────────────────────────────────────────────────────────

def run_tracker():
    print("🏏 IPL Tracker started (no API key)")
    send_alert("🏏 <b>IPL Tracker active!</b>\nAlerts: 50s · 100s")

    match_id, match_name = None, None
    prev_batters:    dict = {}
    milestones_sent: set  = set()
    first_poll            = False

    while True:
        if not match_id:
            new_id, new_name = get_live_match()
            if not new_id:
                print(f"No live match. Retrying in {NO_MATCH_SLEEP // 60} min...")
                time.sleep(NO_MATCH_SLEEP)
                continue
            match_id, match_name = new_id, new_name
            send_alert(f"🏏 <b>Match Started!</b>\n{match_name}")
            prev_batters.clear()
            milestones_sent.clear()
            first_poll = True

        if is_match_complete(match_id):
            send_alert(f"🏁 <b>Match Ended!</b>\n{match_name}")
            match_id, match_name = None, None
            time.sleep(NO_MATCH_SLEEP)
            continue

        check_events(match_id, prev_batters, milestones_sent,
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
