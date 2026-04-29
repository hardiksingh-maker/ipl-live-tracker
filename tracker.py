#!/usr/bin/env python3
"""
IPL Live Tracker — no API key required.
Scrapes Cricbuzz directly. Alerts for powerplay score, 50s and 100s.
"""
import re
import time
import random
import threading
import datetime
import requests
from http.server import HTTPServer, BaseHTTPRequestHandler
import os

TELEGRAM_TOKEN  = "8718609997:AAGlMGxsgZSv0PlPTzqMl_R29NQ-bf3-STI"
CHAT_IDS        = ["5023801264", "1372959952"]
POLL_INTERVAL   = 15            # seconds — alert fires within 15s of milestone
NO_MATCH_SLEEP  = 600           # 10 min when no live match
PORT            = int(os.environ.get("PORT", 10000))

CT_ACCOUNT_ID   = "KKW-674-856Z"
CT_PASSCODE     = "CHW-SMA-CPUL"
CT_SEGMENT_ID   = 1777032259
CT_URL          = "https://eu1.api.clevertap.com/1/targets/create.json"

IST             = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
PUSH_AFTER_IST  = (20, 20)   # 8:20 PM — no push before this
PUSH_DELAY      = 600        # 10-min delay after milestone detection
MAX_PUSHES      = 3          # max CleverTap pushes per match

_push_count = 0
_push_lock  = threading.Lock()

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
    server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
    server.socket.setsockopt(1, 2, 1)  # SO_REUSEADDR — prevents "address already in use" on restart
    server.serve_forever()

RENDER_URL = "https://ipl-live-tracker.onrender.com"

def self_ping():
    time.sleep(60)
    while True:
        try:
            requests.get(RENDER_URL, timeout=10)
        except Exception:
            pass
        time.sleep(240)  # every 4 min — well under Render's 15-min sleep threshold


# ── Telegram ──────────────────────────────────────────────────────────────────

def send_telegram(text: str):
    for chat_id in CHAT_IDS:
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                data={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
                timeout=10,
            )
        except Exception as e:
            print(f"[Telegram error] {e}")


# ── CleverTap ─────────────────────────────────────────────────────────────────

def send_clevertap(title: str, body: str):
    try:
        r = requests.post(
            CT_URL,
            headers={
                "X-CleverTap-Account-Id": CT_ACCOUNT_ID,
                "X-CleverTap-Passcode":   CT_PASSCODE,
                "Content-Type":           "application/json",
            },
            json={
                "name":    f"{title[:40]} — {int(time.time())}",
                "when":    "now",
                "segment": CT_SEGMENT_ID,
                "content": {"title": title, "body": body},
                "devices": ["android", "ios"],
            },
            timeout=10,
        )
        print(f"[CleverTap] {r.status_code}: {r.text[:120]}")
    except Exception as e:
        print(f"[CleverTap error] {e}")


# ── Alerts ───────────────────────────────────────────────────────────────────

def send_alert(text: str, include_ct: bool = False):
    """
    Immediate alert — used for match start/end/tracker status.
    include_ct=True sends CleverTap immediately as well (for match start).
    Never counts against the milestone cap.
    """
    send_telegram(text)
    if include_ct:
        lines = re.sub(r"<[^>]+>", "", text).strip().splitlines()
        title = lines[0].strip() if lines else text[:40]
        body  = lines[1].strip() if len(lines) >= 2 else ""
        send_clevertap(title, body)
    print(f"[ALERT] {text.splitlines()[0]}")


def send_milestone_alert(text: str):
    """
    Milestone alert (50/100).

    Timing rules:
    - Detected before 8:20 PM  →  send at exactly 8:20 PM
    - Detected at/after 8:20 PM →  send 10 min after detection

    Both Telegram and CleverTap fire at the same calculated time.
    Capped at MAX_PUSHES (3) per match — excess milestones are dropped.
    """
    lines = re.sub(r"<[^>]+>", "", text).strip().splitlines()
    title = lines[0].strip() if lines else text[:40]
    body  = lines[1].strip() if len(lines) >= 2 else ""

    detected_at = datetime.datetime.now(IST)
    cutoff      = detected_at.replace(hour=PUSH_AFTER_IST[0], minute=PUSH_AFTER_IST[1],
                                      second=0, microsecond=0)

    if detected_at < cutoff:
        send_at = cutoff                                                      # hold until 8:20 PM
    else:
        send_at = detected_at + datetime.timedelta(seconds=PUSH_DELAY)       # +10 min

    wait_secs = max(0, (send_at - detected_at).total_seconds())
    print(f"[MILESTONE] {title} — scheduled at {send_at.strftime('%H:%M')} IST "
          f"(wait {int(wait_secs)}s)")

    def _fire():
        global _push_count
        time.sleep(wait_secs)
        with _push_lock:
            if _push_count >= MAX_PUSHES:
                print(f"[MILESTONE SKIP] Cap {MAX_PUSHES} reached — dropped: {title}")
                return
            send_telegram(text)
            send_clevertap(title, body)
            _push_count += 1
            print(f"[MILESTONE SENT] {_push_count}/{MAX_PUSHES}: {title}")

    threading.Thread(target=_fire, daemon=True).start()


# ── Cricbuzz scraper ──────────────────────────────────────────────────────────

def _fetch_rsc(url: str) -> str:
    try:
        r = requests.get(url, headers=HDR, timeout=20)
        raw = r.text

        # Extract Next.js RSC payload chunks
        chunks = re.findall(r'self\.__next_f\.push\(\[1,"(.+?)"\]\)', raw, re.DOTALL)
        if chunks:
            combined = "".join(chunks)
            # Decode unicode escapes safely — errors='replace' never corrupts the string
            try:
                combined = combined.encode("utf-8").decode("unicode_escape", errors="replace")
            except Exception:
                pass  # use raw combined if decode raises
            if combined.strip():
                return combined

        # Fallback: return the full page source so field-level regexes still have a chance
        print(f"[Fetch] RSC chunks empty for {url.split('/')[-1]} — falling back to raw HTML")
        return raw
    except Exception as e:
        print(f"[Fetch error] {url}: {e}")
        return ""


DEAD_STATES = {"complete", "preview", "upcoming", ""}

IPL_KEYWORDS = ["indian premier league", "ipl 20", "ipl2"]

def get_live_match() -> tuple:
    """Returns (match_id, match_name) for the current live IPL match."""
    data = _fetch_rsc(f"{BASE}/cricket-match/live")
    if not data:
        return None, None

    # Deduplicated match IDs in page order
    match_ids = list(dict.fromkeys(re.findall(r'"matchId":(\d+)', data)))
    print(f"[Scraper] {len(match_ids)} match IDs found on live page")

    for mid in match_ids:
        needle = f'"matchId":{mid}'
        start  = 0
        found  = False

        # Check every occurrence of this matchId — pick the one that has
        # both "state" AND series info within a tight 2000-char window
        while True:
            idx = data.find(needle, start)
            if idx == -1:
                break
            ctx = data[max(0, idx - 500): idx + 1500]
            start = idx + len(needle)

            sv = re.search(r'"state"\s*:\s*"([^"]+)"', ctx)
            if not sv:
                continue
            st = sv.group(1).lower()
            if st in DEAD_STATES:
                found = True  # state exists but dead — no need to check other occurrences
                break

            sn = re.search(r'"seriesName"\s*:\s*"([^"]+)"', ctx)
            if not sn:
                continue  # seriesName not in this occurrence's window — try next

            series = sn.group(1).lower()
            if not any(kw in series for kw in IPL_KEYWORDS):
                print(f"[Scraper] matchId={mid} state={st!r} series={sn.group(1)!r} — not IPL")
                found = True
                break

            # ── Live IPL match found ──
            t_names = re.findall(r'"teamSName"\s*:\s*"([^"]+)"', ctx)
            t1n  = t_names[0] if len(t_names) > 0 else "?"
            t2n  = t_names[1] if len(t_names) > 1 else "?"
            name = f"{t1n} vs {t2n} — {sn.group(1)}"
            print(f"[IPL Live] matchId={mid} state={st!r}: {name}")
            return mid, name

        if not found:
            pass  # matchId appeared only in non-state contexts — skip silently

    print(f"[Scraper] No live IPL match found among {len(match_ids)} matches")
    return None, None


def get_scorecard(match_id: str) -> dict:
    data = _fetch_rsc(f"{BASE}/live-cricket-scorecard/{match_id}")
    if not data:
        return {}

    batters = {}

    # Approach 1: find "batName" then look for "runs" within the next 300 chars
    for m in re.finditer(r'"batName"\s*:\s*"([^"]+)"', data):
        name = m.group(1)
        nearby = data[m.end(): m.end() + 300]
        rm = re.search(r'"runs"\s*:\s*"?(\d+)"?', nearby)
        if rm:
            runs = int(rm.group(1))
            if runs > batters.get(name, -1):
                batters[name] = runs

    # Approach 2 (fallback): broader name/r pattern used in Cricbuzz's newer format
    if not batters:
        for m in re.finditer(r'"batName"\s*:\s*"([^"]+)"', data):
            name = m.group(1)
            nearby = data[m.end(): m.end() + 300]
            rm = re.search(r'"r"\s*:\s*"?(\d+)"?', nearby)
            if rm:
                runs = int(rm.group(1))
                if runs > batters.get(name, -1):
                    batters[name] = runs

    if not batters:
        print(f"[Scorecard] No batter data found for match {match_id}")
        return {}
    return {"batters": batters}


def is_match_complete(match_id: str) -> bool:
    data = _fetch_rsc(f"{BASE}/cricket-match/live")
    if not data:
        return False  # network failure → assume still live, don't end match prematurely
    idx = data.find(f'"matchId":{match_id}')
    if idx == -1:
        return True  # match no longer in live feed → genuinely over
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
                send_milestone_alert(fifty_msg(name, runs))
                milestones_sent.add(f"{name}_50")

            if prev_r < 100 <= runs and f"{name}_100" not in milestones_sent:
                send_milestone_alert(century_msg(name, runs))
                milestones_sent.add(f"{name}_100")

    prev_batters.clear()
    prev_batters.update(curr_batters)


# ── Tracker loop ──────────────────────────────────────────────────────────────

def run_tracker():
    print("🏏 IPL Tracker started (no API key)")
    send_alert("🏏 <b>IPL Tracker active!</b>\nAlerts: 50s · 100s")

    match_id,     match_name   = None, None
    prev_batters: dict         = {}
    milestones_sent: set       = set()
    seen_match_ids: set        = set()   # prevents re-alerting same match on restart
    first_poll                 = False

    while True:
        if not match_id:
            new_id, new_name = get_live_match()
            if not new_id:
                print(f"No live match. Retrying in {NO_MATCH_SLEEP // 60} min...")
                time.sleep(NO_MATCH_SLEEP)
                continue

            if new_id in seen_match_ids:
                # same match still appearing as live after we marked it done — wait it out
                print(f"[SKIP] Match {new_id} already processed. Waiting...")
                time.sleep(NO_MATCH_SLEEP)
                continue

            match_id, match_name = new_id, new_name
            seen_match_ids.add(match_id)
            send_alert(f"🏏 <b>Match Started!</b>\n{match_name}", include_ct=True)
            prev_batters.clear()
            milestones_sent.clear()
            global _push_count
            _push_count = 0
            first_poll  = True

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
