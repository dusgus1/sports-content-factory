#!/usr/bin/env python3
"""
Tennis AI Agent — monitors ATP/WTA matches 24/7 and posts results to Telegram.
Strategy: every 5 min, scan past-matches for all active players (derived from
this week's fixture schedule) and post any completed results not yet posted.
"""

import os
import json
import logging
import hashlib
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger
from dotenv import load_dotenv

# ── Config ───────────────────────────────────────────────────────────────────
load_dotenv()

RAPIDAPI_KEY        = os.environ["RAPIDAPI_KEY"]
RAPIDAPI_HOST       = os.environ["RAPIDAPI_HOST"]
MINIMAX_API_KEY     = os.environ["MINIMAX_API_KEY"]
MINIMAX_BASE_URL    = os.environ["MINIMAX_BASE_URL"].rstrip("/")
TELEGRAM_BOT_TOKEN  = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHANNEL_ID = os.environ["TELEGRAM_CHANNEL_ID"]

POSTED_FILE = Path("/root/tennis-agent/posted_matches.json")
LOG_FILE    = Path("/root/tennis-agent/agent.log")

RAPIDAPI_BASE    = f"https://{RAPIDAPI_HOST}"
RAPIDAPI_HEADERS = {
    "x-rapidapi-key":  RAPIDAPI_KEY,
    "x-rapidapi-host": RAPIDAPI_HOST,
}

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# ── Tournament quality filter ─────────────────────────────────────────────────
# Tier strings that are always posted regardless of player ranking
_PREMIUM_TIER_KW = (
    "grand slam",
    "masters 1000", "atp masters",      # ATP Masters 1000
    "atp 500", "atp 250",               # ATP 500 / 250
    "wta 1000", "wta 500", "wta 250", # WTA equivalents
    "premier mandatory", "premier 5",   # legacy WTA Premier naming
)
# Grand Slam tournament names (fallback when tier field is missing/inconsistent)
_GRAND_SLAM_NAMES = (
    "australian open", "roland garros", "french open",
    "wimbledon", "us open",
)

def should_post(tournament: dict, w_rank: int | None, l_rank: int | None) -> tuple[bool, str]:
    """
    Returns (post_it, reason_string).
    Premium tiers  → always True.
    Challenger/ITF → True only when at least one player is ranked ≤ 100.
    Unknown tier / no rank data → skip.
    """
    tier = (tournament.get("tier") or "").lower()
    name = (tournament.get("name") or "").lower()

    # Grand Slam by name (belt-and-suspenders against missing tier data)
    if any(gs in name for gs in _GRAND_SLAM_NAMES):
        return True, "Grand Slam"

    # Premium tier match
    for kw in _PREMIUM_TIER_KW:
        if kw in tier:
            return True, f"premium tier ({tournament.get('tier')})"

    # "Masters" anywhere in name catches edge cases like "BNP Paribas Masters"
    if "masters" in name and "1000" not in name:
        # could be Masters 1000 or just "Masters" branding on a lower event
        # be generous: treat any "Masters" as premium
        return True, "Masters name match"

    # Everything else (Challenger, ITF W-series, …):
    # require at least one top-150 player; skip if no rank known
    ranks = [r for r in (w_rank, l_rank) if r and r > 0]
    best  = min(ranks) if ranks else None
    if not best:
        return False, "no ranking data available"
    if best <= 150:
        return True, f"non-premium with top-150 player (best rank #{best})"

    reason = (
        f"tier={tournament.get('tier')!r} name={tournament.get('name')!r} "
        f"w_rank={w_rank} l_rank={l_rank}"
    )
    return False, reason


# ── In-memory caches ─────────────────────────────────────────────────────────
_round_names:      dict[int, str]  = {}
_tournament_cache: dict[int, dict] = {}
_player_cache:     dict[int, dict] = {}
_watchlist:        set              = set()   # (tour, player_id)
_watchlist_built:  str             = ""       # date it was last built
_skipped_uids:     set             = set()    # matches skipped by filter this session


# ── Posted-match tracker ─────────────────────────────────────────────────────
def load_posted() -> set:
    if POSTED_FILE.exists():
        try:
            return set(json.loads(POSTED_FILE.read_text()))
        except Exception:
            return set()
    return set()


def save_posted(ids: set) -> None:
    POSTED_FILE.write_text(json.dumps(list(ids), indent=2))


def result_uid(tour: str, m: dict) -> str:
    key = (
        tour
        + str(m.get("player1Id", ""))
        + str(m.get("player2Id", ""))
        + str(m.get("result", ""))
        + (m.get("date") or "")[:10]
    )
    return hashlib.sha256(key.encode()).hexdigest()[:20]


# ── RapidAPI helper ───────────────────────────────────────────────────────────
def api_get(path: str, retries: int = 3) -> dict | None:
    url = RAPIDAPI_BASE + path
    time.sleep(1.1)   # respect ~100 req/min limit (≈ 0.6 req/sec; 1.1 s gives ~54/min, safely under limit)
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, headers=RAPIDAPI_HEADERS, timeout=15)
            if r.status_code == 429:
                log.warning("Rate-limited — sleeping 60 s")
                time.sleep(60)
                continue
            if r.status_code != 200:
                log.debug("API %s → %s", path, r.status_code)
                return None
            return r.json()
        except requests.RequestException as exc:
            log.warning("Request error (%d/%d): %s", attempt, retries, exc)
            if attempt < retries:
                time.sleep(4 * attempt)
    return None


# ── Lookup helpers ────────────────────────────────────────────────────────────
def get_round_names() -> None:
    global _round_names
    if _round_names:
        return
    data = api_get("/tennis/v2/round")
    if data and isinstance(data.get("data"), list):
        _round_names = {r["round_id"]: r["round_name"] for r in data["data"]}
        log.info("Loaded %d round names", len(_round_names))


def get_tournament(tour: str, tid: int) -> dict:
    if tid in _tournament_cache:
        return _tournament_cache[tid]
    data = api_get(f"/tennis/v2/{tour}/tournament/info/{tid}")
    info: dict = {}
    if data and "data" in data:
        d = data["data"]
        info = {
            "name":    d.get("name", ""),
            "surface": (d.get("court") or {}).get("name", ""),
            "tier":    d.get("tier", ""),
        }
    _tournament_cache[tid] = info
    return info


def get_player(tour: str, pid: int) -> dict:
    if pid in _player_cache:
        return _player_cache[pid]
    data = api_get(f"/tennis/v2/{tour}/player/profile/{pid}")
    info: dict = {}
    if data and "data" in data:
        d = data["data"]
        info = {"name": d.get("name", ""), "rank": d.get("currentRank")}
    _player_cache[pid] = info
    return info


def get_h2h_summary(tour: str, winner_id: int, loser_id: int,
                    w_name: str, l_name: str) -> str:
    data = api_get(f"/tennis/v2/{tour}/player/past-matches/{winner_id}")
    if not data:
        return ""
    w_wins = l_wins = 0
    for m in (data.get("data") or []):
        p1 = m.get("player1Id")
        p2 = m.get("player2Id")
        if {p1, p2} != {winner_id, loser_id}:
            continue
        if m.get("match_winner") == winner_id:
            w_wins += 1
        else:
            l_wins += 1
    if w_wins + l_wins == 0:
        return ""
    return f"H2H: {w_name.split()[-1]} {w_wins}-{l_wins} {l_name.split()[-1]} (career)"


# ── Player watch list ─────────────────────────────────────────────────────────
def build_watchlist() -> None:
    """
    Rebuild the set of (tour, player_id) to monitor.
    Pulls fixtures for: today, yesterday, and tomorrow — so we catch
    players who played earlier today (already gone from 'today' fixtures)
    and late starters.
    Rebuilds once per calendar day.
    """
    global _watchlist, _watchlist_built
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if _watchlist_built == today_str and _watchlist:
        return

    new_watch: set = set()
    now = datetime.now(timezone.utc)
    # Only today + yesterday — tomorrow's players haven't played yet, no need to check them
    dates = [
        (now - timedelta(days=1)).strftime("%Y-%m-%d"),
        today_str,
    ]

    for tour in ("atp", "wta"):
        # Undated endpoint = today's upcoming (players still to play today)
        data = api_get(f"/tennis/v2/{tour}/fixtures")
        for m in (data.get("data") or []) if data else []:
            new_watch.add((tour, m["player1Id"]))
            new_watch.add((tour, m["player2Id"]))

        # Date-specific fixtures for today + yesterday
        # (catches players whose completed matches are now gone from the undated endpoint)
        for d in dates:
            data2 = api_get(f"/tennis/v2/{tour}/fixtures/{d}")
            for m in (data2.get("data") or []) if data2 else []:
                new_watch.add((tour, m["player1Id"]))
                new_watch.add((tour, m["player2Id"]))

    _watchlist       = new_watch
    _watchlist_built = today_str
    log.info("Watch list built: %d player-tour pairs", len(_watchlist))


def build_daily_schedule() -> None:
    """
    Every morning: fetch today's fixture list from API and log all matches
    so we can verify the agent is tracking the right games.
    Runs once per calendar day.
    """
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if getattr(build_daily_schedule, '_built_date', None) == today_str:
        return
    build_daily_schedule._built_date = today_str

    log.info("=== TODAY'S MATCH SCHEDULE (%s) ===", today_str)
    total = 0
    for tour in ("atp", "wta"):
        data = api_get(f"/tennis/v2/{tour}/fixtures/{today_str}")
        matches = (data.get("data") or []) if data else []
        tracked = []
        skipped = []
        for m in matches:
            p1 = (m.get("player1") or {}).get("name", "?")
            p2 = (m.get("player2") or {}).get("name", "?")
            if "/" in p1 or "/" in p2:
                continue
            tid = m.get("tournamentId")
            t_info = get_tournament(tour, tid) if tid else {}
            t_name = t_info.get("name", "Unknown")
            t_tier = t_info.get("tier", "")
            tier_lower = t_tier.lower()
            is_premium = any(kw in tier_lower for kw in [
                "grand slam", "masters", "atp 500", "atp 250",
                "wta 1000", "wta 500", "wta 250"
            ])
            if is_premium:
                tracked.append(f"  ✅ [{tour.upper()}] {p1} vs {p2} — {t_name} ({t_tier})")
            else:
                skipped.append(f"  ⏭ [{tour.upper()}] {p1} vs {p2} — {t_name} ({t_tier})")

        if tracked:
            log.info("[%s] TRACKING %d matches:", tour.upper(), len(tracked))
            for line in tracked:
                log.info(line)
        if skipped:
            log.info("[%s] LOW-TIER (will post only if top-150 player): %d matches", tour.upper(), len(skipped))
        total += len(tracked)

    log.info("=== SCHEDULE DONE — %d premium matches today ===", total)


# ── Upset detection ───────────────────────────────────────────────────────────
def detect_upset(fixture_seed_winner: str | None, fixture_seed_loser: str | None,
                 w_rank: int | None, l_rank: int | None) -> bool:
    try:
        sw = int(fixture_seed_winner) if (fixture_seed_winner or "").isdigit() else 999
        sl = int(fixture_seed_loser)  if (fixture_seed_loser  or "").isdigit() else 999
        if sw > sl and sl <= 8:
            return True
    except Exception:
        pass
    if w_rank and l_rank and w_rank > l_rank + 30:
        return True
    return False


# ── Post generation ───────────────────────────────────────────────────────────
def generate_post(
    w_name: str, l_name: str, score: str,
    tournament: dict, round_name: str, h2h: str,
    w_rank: int | None, l_rank: int | None,
    upset: bool, tour: str,
) -> str:
    tour_tag = "#ATP" if tour == "atp" else "#WTA"
    t_name   = tournament.get("name") or "ATP/WTA Tour"
    surface  = tournament.get("surface") or ""
    tier     = tournament.get("tier") or ""

    ctx_parts = [
        f"Winner: {w_name}",
        f"Loser: {l_name}",
        f"Score: {score}",
        f"Tournament: {t_name}" + (f" ({tier})" if tier else ""),
    ]
    if round_name:
        ctx_parts.append(f"Round: {round_name}")
    if surface:
        ctx_parts.append(f"Surface: {surface}")
    if w_rank:
        ctx_parts.append(f"Winner world ranking: #{w_rank}")
    if l_rank:
        ctx_parts.append(f"Loser world ranking: #{l_rank}")
    if h2h:
        ctx_parts.append(h2h)
    context = "\n".join(ctx_parts)

    if upset:
        instruction = (
            "You are a sharp tennis journalist writing for a viral sports Telegram channel.\n"
            "Write a punchy breaking-news style post about this upset.\n\n"
            "Rules:\n"
            "- Start with 🚨 BREAKING: or 🚨 UPSET: — make the first line a hook that grabs attention\n"
            "- Mention WHY this is notable — ranking gap, seeding, tournament stage, surface context\n"
            "- If the loser is a known top player, make THEM the story (\\\"Medvedev crashes out\\\", \\\"Swiatek stunned\\\")\n"
            "- Add one line of context or a sharp observation (e.g. \\\"First loss on grass this season\\\" or \\\"Knocked out in R1 for the second year running\\\")\n"
            "- End with relevant hashtags on the last line\n"
            "- Max 5 lines total. No fluff. No \\\"It was a great match\\\". Punchy and viral.\n"
            "- English only."
        )
    else:
        instruction = (
            "You are a sharp tennis journalist writing viral Telegram posts. "
            "Write a punchy, engaging post about this match result.\n\n"
            "RULES:\n"
            "- Line 1: Start with 🎾 + a HOOK headline. Make it interesting and specific.\n"
            "  If FINAL → focus on title win: '🎾 Humbert claims his first grass title at Eastbourne'\n"
            "  If top-10 player → make them the story: '🎾 Sabalenka rolls into the QF without dropping serve'\n"
            "  If comeback → mention it: '🎾 Fokina survives a set down to advance in Mallorca'\n"
            "  NEVER write 'RESULT |' or just player names as the headline\n"
            "- Line 2: Winner def. Loser + score\n"
            "- Line 3: Tournament · Round · Surface (compact, one line)\n"
            "- Line 4: ONE sharp stat or context. Examples:\n"
            "  'Humbert is now 5-0 in grass-court semifinals this season'\n"
            "  'Keys wins her first title since 2019'\n"
            "  Skip if nothing notable.\n"
            "- Last line: hashtags only\n"
            "- MAX 5 lines. No filler. No 'great match'. Punchy. English only.\n"
        )

    try:
        resp = requests.post(
            f"{MINIMAX_BASE_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {MINIMAX_API_KEY}",
                "Content-Type":  "application/json",
            },
            json={
                "model":       "MiniMax-M2.1-highspeed",
                "messages":    [{"role": "user", "content": instruction + "\n\nMatch data:\n" + context}],
                "max_tokens":  2000,   # reasoning models need ~900 tokens to think before output
                "temperature": 0.8,
            },
            timeout=60,
        )
        resp.raise_for_status()
        raw = resp.json()["choices"][0]["message"]["content"]
        # Strip internal <think>…</think> reasoning block — keep only the final answer
        import re as _re
        clean = _re.sub(r"<think>.*?</think>", "", raw, flags=_re.DOTALL).strip()
        return clean if clean else _fallback_post(w_name, l_name, score, t_name, round_name,
                                                   surface, h2h, w_rank, l_rank, upset, tour_tag)
    except Exception as exc:
        log.error("MiniMax error: %s", exc)
        return _fallback_post(w_name, l_name, score, t_name, round_name,
                              surface, h2h, w_rank, l_rank, upset, tour_tag)


def _fallback_post(w_name, l_name, score, t_name, round_name,
                   surface, h2h, w_rank, l_rank, upset, tour_tag) -> str:
    if upset:
        lines = [
            f"🚨 UPSET: {l_name} ({f'#{l_rank}' if l_rank else 'seeded'}) stunned by {w_name} ({f'#{w_rank}' if w_rank else 'unranked'})",
            f"{w_name} def. {l_name} {score}",
            f"{t_name}",
            f"#TennisUpset {tour_tag}"
        ]
    else:
        if round_name and "final" in round_name.lower():
            headline = f"{w_name} wins the {t_name} title"
        elif round_name and surface:
            headline = f"{w_name} advances at {t_name}"
        else:
            headline = f"{w_name} beats {l_name} at {t_name}"
        lines = [f"🎾 {headline}", f"{w_name} def. {l_name} {score}"]
        meta = " · ".join(filter(None, [round_name, surface]))
        if meta:
            lines.append(meta)
        if h2h:
            lines.append(h2h)
        lines.append(f"#Tennis {tour_tag}")
    return "\n".join(lines)


# ── Telegram sender ───────────────────────────────────────────────────────────
def send_telegram(text: str) -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for attempt in range(1, 4):
        try:
            r = requests.post(url, json={"chat_id": TELEGRAM_CHANNEL_ID, "text": text}, timeout=15)
            if r.status_code == 200:
                log.info("Telegram sent OK")
                return True
            log.warning("Telegram %s: %s", r.status_code, r.text[:200])
        except requests.RequestException as exc:
            log.warning("Telegram error (%d/3): %s", attempt, exc)
        time.sleep(3 * attempt)
    return False


# ── Main check loop ───────────────────────────────────────────────────────────
def check_matches() -> None:
    log.info("=== Check cycle started ===")
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    posted    = load_posted()

    build_watchlist()
    build_daily_schedule()

    seen_pairs: set = set()  # canonical (tour, min_id, max_id) — avoid double-posting same match
    new_posts = 0

    for (tour, player_id) in list(_watchlist):
        data = api_get(f"/tennis/v2/{tour}/player/past-matches/{player_id}")
        if not data:
            time.sleep(0.3)
            continue

        for m in (data.get("data") or []):
            if m.get("result_type") != "completed":
                continue
            match_date = (m.get("date") or "")[:10]
            if match_date < today_str:
                break   # newest-first; nothing older will be today
            if match_date != today_str:
                continue

            p1id = m.get("player1Id")
            p2id = m.get("player2Id")
            if not p1id or not p2id:
                continue

            # Skip doubles matches
            p1_name = (m.get("player1") or {}).get("name", "")
            p2_name = (m.get("player2") or {}).get("name", "")
            if "/" in p1_name or "/" in p2_name:
                continue

            pair = (tour, min(p1id, p2id), max(p1id, p2id))
            if pair in seen_pairs:
                continue

            uid = result_uid(tour, m)
            if uid in posted:
                seen_pairs.add(pair)
                continue

            seen_pairs.add(pair)

            # ── Gather match details ──────────────────────────────────────
            winner_id  = m.get("match_winner") or p1id
            loser_id   = p2id if winner_id == p1id else p1id
            score      = m.get("result", "N/A")
            round_id   = m.get("roundId")
            round_name = _round_names.get(round_id, "") if round_id else ""
            t_id       = m.get("tournamentId")

            w_name = (m.get("player1") or {}).get("name") or ""
            l_name = (m.get("player2") or {}).get("name") or ""

            log.info("Found completed: %s def. %s %s [%s]",
                     w_name or winner_id, l_name or loser_id, score, tour.upper())

            tournament = get_tournament(tour, t_id) if t_id else {}
            w_info = get_player(tour, winner_id)
            l_info = get_player(tour, loser_id)

            if not w_name:
                w_name = w_info.get("name", str(winner_id))
            if not l_name:
                l_name = l_info.get("name", str(loser_id))
            w_rank = w_info.get("rank")
            l_rank = l_info.get("rank")

            # ── Tournament quality filter ─────────────────────────────────
            post_ok, filter_reason = should_post(tournament, w_rank, l_rank)
            if not post_ok:
                if uid not in _skipped_uids:
                    log.info(
                        "SKIP: %s def. %s [%s] — %s",
                        w_name, l_name, tour.upper(), filter_reason,
                    )
                    _skipped_uids.add(uid)
                continue

            log.info(
                "PASS filter: %s def. %s [%s] — %s",
                w_name, l_name, tour.upper(), filter_reason,
            )

            # Detect upset (by world ranking; no seeding in past-matches)
            upset = detect_upset(None, None, w_rank, l_rank)
            if upset:
                log.info("UPSET detected: %s (#%s) beat %s (#%s)",
                         w_name, w_rank, l_name, l_rank)

            h2h = get_h2h_summary(tour, winner_id, loser_id, w_name, l_name)

            post_text = generate_post(
                w_name, l_name, score,
                tournament, round_name, h2h,
                w_rank, l_rank, upset, tour,
            )
            log.info("Post:\n%s", post_text)

            if send_telegram(post_text):
                posted.add(uid)
                save_posted(posted)
                new_posts += 1
                time.sleep(2)   # Telegram flood guard

        # api_get already sleeps 1.1 s per call; no extra sleep needed here

    log.info("=== Cycle done — %d new post(s) ===", new_posts)


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    log.info("Tennis AI Agent starting up...")
    get_round_names()
    check_matches()
    log.info("Startup complete. Monitoring every 5 minutes.")

    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(
        check_matches,
        trigger=IntervalTrigger(minutes=5),
        id="tennis_check",
        name="Check tennis results",
        misfire_grace_time=60,
    )
    log.info("Scheduler active — checking every 5 minutes")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Agent stopped")
