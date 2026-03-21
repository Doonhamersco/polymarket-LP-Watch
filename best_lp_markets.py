#!/usr/bin/env python3
"""
Polymarket LP Rewards — Best Low-Risk Markets

Fetches active Polymarket markets with LP rewards, scores them by risk
(spike, time proximity, adverse selection), and displays the best markets
where risk is minimal for farming LP rewards.

Based on lp.md (Polymarket LP Rewards Analyzer).
"""

import json
import re
import sys
import time
import urllib.request
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

# --- Constants ---
GAMMA_BASE = "https://gamma-api.polymarket.com/markets"
GAMMA_EVENTS_BASE = "https://gamma-api.polymarket.com/events"
DATA_API_BASE = "https://data-api.polymarket.com/positions"
PAGE_LIMIT = 100
REQUEST_TIMEOUT = 30
# Gamma "series" id for NCAA men's CBB (see GET /sports — sport "cbb", series "10470" / ncaa-cbb)
CBB_SERIES_ID = 10470
# Terminal/Telegram: show bids-before in red when below this USD (in addition to min_bid_depth alert)
BIDS_BEFORE_DISPLAY_RED_USD = 1_200_000.0
# Show markets with composite risk score at or below this (0–100; lower = safer)
MAX_RISK_FOR_DISPLAY = 35
# Max number of "best" low-risk markets to show
TOP_N = 25

POSITIONS_PATH = Path(__file__).with_name("positions.json")
MONITOR_CONFIG_PATH = Path(__file__).with_name("monitor_config.json")

# Track chats that are expected to send bulk position input next
BULK_INPUT_PENDING: dict[str, bool] = {}

# Simple ANSI colors for nicer terminal output (no external deps)
RESET = "\033[0m"
BOLD = "\033[1m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
CYAN = "\033[36m"
ORANGE = "\033[38;5;208m"  # 256-color orange (sports countdown 4–7h)
USE_COLOR = sys.stdout.isatty()


def color_text(text: str, color: str) -> str:
    """Wrap text in an ANSI color if supported."""
    if not USE_COLOR:
        return text
    return f"{color}{text}{RESET}"


def parse_iso_datetime_to_utc(value: object) -> Optional[datetime]:
    """Parse Polymarket/Gamma date strings to timezone-aware UTC."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s.replace("Z", "+00:00")
    elif " " in s and "T" not in s[:20]:
        s = s.replace(" ", "T", 1)
    try:
        dt = datetime.fromisoformat(s)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt


def parse_market_game_start_utc(market: dict) -> Optional[datetime]:
    """
    Best-effort game / event start for sports markets from Gamma API.
    Uses gameStartTime, nested event startTime, then endDate (often tip-off for CBB).
    """
    gst = market.get("gameStartTime")
    if gst:
        dt = parse_iso_datetime_to_utc(gst)
        if dt is not None:
            return dt
    events = market.get("events") or []
    if isinstance(events, list) and events:
        ev = events[0] or {}
        for key in ("startTime", "endDate"):
            dt = parse_iso_datetime_to_utc(ev.get(key))
            if dt is not None:
                return dt
    return parse_iso_datetime_to_utc(market.get("endDate"))


def format_game_countdown_colored(
    game_start: Optional[datetime], now_utc: datetime
) -> str:
    """
    Human-readable countdown with color:
    - Red: game started or &lt; 4 hours until start (exit window)
    - Orange: 4–7 hours until start
    - Green: 7+ hours until start
    """
    if game_start is None:
        return color_text("game: time unknown", CYAN)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    delta = game_start - now_utc
    sec = delta.total_seconds()
    if sec <= 0:
        return color_text("GAME STARTED", RED)
    hours = sec / 3600.0
    h_int = int(sec // 3600)
    m_int = int((sec % 3600) // 60)
    if h_int >= 1:
        label = f"{h_int} HOURS UNTIL GAME" if m_int == 0 else f"{h_int}h {m_int}m UNTIL GAME"
    else:
        label = f"{m_int} MIN UNTIL GAME" if m_int > 0 else "SOON"
    if hours < 4.0:
        return color_text(label, RED)
    if hours < 7.0:
        return color_text(label, ORANGE)
    return color_text(label, GREEN)


def format_bids_before_terminal(bb: float, min_alert_usd: float) -> str:
    """Color bids-before: red below $1.2M; ⚠ when also below min alert threshold."""
    if bb < min_alert_usd:
        return color_text(f"${bb:,.2f} ⚠", RED)
    if bb < BIDS_BEFORE_DISPLAY_RED_USD:
        return color_text(f"${bb:,.2f}", RED)
    return f"${bb:,.2f}"


def format_bids_before_telegram_html(bids: float) -> str:
    """Telegram HTML for bids before; flag when below $1.2M (no ANSI red in Telegram)."""
    s = f"<b>${bids:,.2f}</b>"
    if bids < BIDS_BEFORE_DISPLAY_RED_USD:
        return s + " ⚠️"
    return s


def position_row_sort_key(row: dict, now_utc: datetime) -> tuple:
    """
    Sort rows for display: soonest upcoming game first, then distance, then bids_before.
    Unknown game times last; games already started group after upcoming.
    """
    gs = row.get("game_start")
    dist = row.get("distance_cents")
    d = dist if dist is not None else 1e9
    bids = row.get("bids_before")
    b = bids if bids is not None else 1e9
    if gs is None:
        return (1e18, d, b)
    g = gs if gs.tzinfo else gs.replace(tzinfo=timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    sec_until = (g - now_utc).total_seconds()
    if sec_until < 0:
        return (1e17, sec_until, d, b)
    return (sec_until, d, b)


# =============================================================================
# Position monitoring / Telegram config
# =============================================================================

@dataclass
class Position:
    market_slug: str
    side: str  # "YES" or "NO"
    my_limit_price: float
    notes: str = ""


class TelegramBot:
    """Minimal Telegram bot client using only urllib."""

    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = str(chat_id) if chat_id is not None else ""
        self.base_url = f"https://api.telegram.org/bot{token}"

    def send_message(self, text: str, parse_mode: str = "HTML") -> bool:
        try:
            payload = {
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True,
            }
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                f"{self.base_url}/sendMessage",
                data=data,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                # If it doesn't raise, assume success
                resp.read()
            return True
        except Exception as e:
            print(f"Telegram send failed: {e}", file=sys.stderr)
            return False

    def get_updates(self, offset: Optional[int] = None, timeout: int = 0) -> list[dict]:
        """Fetch updates for this bot (used for command handling)."""
        try:
            params: dict[str, object] = {}
            if offset is not None:
                params["offset"] = offset
            if timeout:
                params["timeout"] = timeout
            query = urllib.parse.urlencode(params)
            url = f"{self.base_url}/getUpdates"
            if query:
                url = f"{url}?{query}"
            with urllib.request.urlopen(url, timeout=(timeout or 10) + 5) as resp:
                data = json.loads(resp.read().decode())
            return data.get("result", [])
        except Exception as e:
            print(f"Telegram getUpdates failed: {e}", file=sys.stderr)
            return []


def fetch_all_markets(*, quiet: bool = False):
    """Fetch all active, non-closed markets with pagination.

    When ``quiet`` is True, progress lines are not printed (for callers that want a silent fetch).
    """
    all_markets = []
    offset = 0
    while True:
        try:
            url = (
                f"{GAMMA_BASE}?active=true&closed=false"
                f"&limit={PAGE_LIMIT}&offset={offset}"
            )
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "LPScan/1.0 (LP rewards analyzer)"},
            )
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                markets = json.loads(resp.read().decode())
        except (OSError, json.JSONDecodeError) as e:
            print(f"API error at offset {offset}: {e}", file=sys.stderr)
            break
        if not markets:
            break
        all_markets.extend(markets)
        if len(markets) < PAGE_LIMIT:
            break
        offset += PAGE_LIMIT
        if not quiet:
            print(f"  Fetched {len(all_markets)} markets...", flush=True)
    return all_markets


def outcome_to_yes_no_for_market(
    market: dict, outcome_label: str, raw_position: dict
) -> str:
    """Map Data API outcome to YES/NO for this market's token pair."""
    oi = raw_position.get("outcomeIndex")
    if oi is not None:
        try:
            return "YES" if int(oi) == 0 else "NO"
        except (TypeError, ValueError):
            pass
    outcomes = market.get("outcomes")
    if isinstance(outcomes, str):
        try:
            outcomes = json.loads(outcomes.replace("'", '"'))
        except Exception:
            outcomes = []
    if not outcomes or len(outcomes) < 2:
        ol = (outcome_label or "").strip().lower()
        if ol in ("no", "n"):
            return "NO"
        return "YES"
    o0 = str(outcomes[0]).strip()
    o1 = str(outcomes[1]).strip()
    on = (outcome_label or "").strip()
    if on.lower() == o0.lower():
        return "YES"
    if on.lower() == o1.lower():
        return "NO"
    if on.lower() in (o0.lower(), "yes", "y"):
        return "YES"
    if on.lower() in (o1.lower(), "no", "n"):
        return "NO"
    return "YES"


def positions_from_wallet_data_api(user_address: str) -> list[Position]:
    """
    Build monitor positions from Polymarket Data API (on-chain holdings).

    When you sell/close, the position disappears on the next refresh — no manual remove.

    Limitation: **unfilled LP limit orders do not appear** here (only filled holdings).
    For resting bids, keep using positions.json + Telegram or future CLOB order sync.
    """
    raw = fetch_user_positions(user_address)
    out: list[Position] = []
    for p in raw:
        slug = (p.get("slug") or "").strip()
        if not slug:
            continue
        try:
            size = float(p.get("size", 0) or 0)
        except (TypeError, ValueError):
            continue
        if size <= 0:
            continue
        market = fetch_market_by_slug(slug)
        if not market:
            continue
        side = outcome_to_yes_no_for_market(market, p.get("outcome") or "", p)
        ap = float(p.get("avgPrice", 0) or 0)
        cp = float(p.get("curPrice", 0) or 0)
        ref = ap if ap > 0 else cp
        if ref <= 0:
            ref = 0.01
        out.append(
            Position(
                market_slug=slug,
                side=side,
                my_limit_price=ref,
                notes="wallet_sync",
            )
        )
    return out


def fetch_user_positions(user_address: str, limit: int = 500) -> list[dict]:
    """
    Fetch current positions for a given Polymarket user/proxy wallet address
    from the public Data API.

    Read-only: requires only the public address (no private key or auth).
    """
    all_positions: list[dict] = []
    offset = 0
    while True:
        try:
            params = {
                "user": user_address,
                "sizeThreshold": 0,
                "limit": limit,
                "offset": offset,
            }
            query = urllib.parse.urlencode(params)
            url = f"{DATA_API_BASE}?{query}"
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "LPScan/1.0 (LP rewards analyzer)"},
            )
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                chunk = json.loads(resp.read().decode())
        except (OSError, json.JSONDecodeError) as e:
            print(f"Data API error at offset {offset}: {e}", file=sys.stderr)
            break
        if not chunk:
            break
        if not isinstance(chunk, list):
            print("Unexpected positions response format from Data API.", file=sys.stderr)
            break
        all_positions.extend(chunk)
        if len(chunk) < limit:
            break
        offset += limit
    return all_positions


def normalize_market_slug(slug: str) -> str:
    """Normalize user input into a Polymarket market slug.

    Accepts:
    - Raw slug:                    'did-a-crypto-hedge-fund-blow-up'
    - Event/market path:           'event-slug/did-a-crypto-hedge-fund-blow-up'
    - Full URL:                    'https://polymarket.com/event/.../did-a-crypto-hedge-fund-blow-up'
    Always returns just the final slug segment.
    """
    slug = (slug or "").strip()
    if not slug:
        return slug
    # Strip full URL if present
    if slug.startswith("http://") or slug.startswith("https://"):
        try:
            parsed = urllib.parse.urlparse(slug)
            path = parsed.path  # e.g. /event/foo/bar
        except Exception:
            path = slug
    else:
        path = slug
    # Take last non-empty segment
    parts = [p for p in path.split("/") if p]
    return parts[-1] if parts else slug


def fetch_market_by_slug(slug: str) -> Optional[dict]:
    """Fetch a single market by slug from Gamma API."""
    try:
        norm_slug = normalize_market_slug(slug)
        query = urllib.parse.urlencode({"slug": norm_slug})
        url = f"{GAMMA_BASE}?{query}"
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "LPScan/1.0 (LP rewards analyzer)"},
        )
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            markets = json.loads(resp.read().decode())
        return markets[0] if markets else None
    except Exception as e:
        print(f"Failed to fetch market by slug '{slug}': {e}", file=sys.stderr)
        return None


def fetch_event_markets(event_slug: str) -> list[dict]:
    """Fetch all sub-markets for a Polymarket event slug via the Gamma events API."""
    try:
        norm_slug = normalize_market_slug(event_slug)
        query = urllib.parse.urlencode({"slug": norm_slug})
        url = f"https://gamma-api.polymarket.com/events?{query}"
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "LPScan/1.0 (LP rewards analyzer)"},
        )
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            events = json.loads(resp.read().decode())
        if not events:
            return []
        return events[0].get("markets", []) or []
    except Exception as e:
        print(f"Failed to fetch event markets for '{event_slug}': {e}", file=sys.stderr)
        return []


def fetch_active_event_slugs_for_series(series_id: int) -> list[str]:
    """Paginate Gamma /events for a series; return unique event slugs (e.g. all active CBB games)."""
    slugs: list[str] = []
    seen: set[str] = set()
    offset = 0
    while True:
        try:
            params = urllib.parse.urlencode(
                {
                    "series_id": series_id,
                    "active": "true",
                    "closed": "false",
                    "limit": PAGE_LIMIT,
                    "offset": offset,
                }
            )
            url = f"{GAMMA_EVENTS_BASE}?{params}"
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "LPScan/1.0 (LP rewards analyzer)"},
            )
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                events = json.loads(resp.read().decode())
        except (OSError, json.JSONDecodeError) as e:
            print(f"API error fetching events at offset {offset}: {e}", file=sys.stderr)
            break
        if not events:
            break
        for e in events:
            s = (e.get("slug") or "").strip()
            if s and s not in seen:
                seen.add(s)
                slugs.append(s)
        if len(events) < PAGE_LIMIT:
            break
        offset += PAGE_LIMIT
    return slugs


def export_ncaa_cbb_market_slugs(
    out_tsv: Optional[Path] = None,
    out_slugs: Optional[Path] = None,
    series_id: int = CBB_SERIES_ID,
) -> None:
    """
    Fetch every active NCAA CBB game in the Gamma series, then each game's sub-markets
    (moneyline, spreads, totals, …) via /events?slug=… — same data as /list_event per game.
    Writes a TSV and a plain slug list for bulk_add / Telegram.
    """
    out_tsv = out_tsv or Path(__file__).with_name("cbb_markets_export.tsv")
    out_slugs = out_slugs or Path(__file__).with_name("cbb_market_slugs.txt")
    print()
    print("Fetching active NCAA CBB event slugs (Gamma series_id=%s)..." % series_id)
    event_slugs = fetch_active_event_slugs_for_series(series_id)
    print(f"Found {len(event_slugs)} event(s). Fetching sub-markets per game (one API call each)...")
    lines_tsv: list[str] = ["event_slug\tmarket_slug\tquestion\tgame_start_time"]
    slug_only: list[str] = []
    seen_market: set[str] = set()
    for i, ev_slug in enumerate(event_slugs, 1):
        if i == 1 or i % 25 == 0 or i == len(event_slugs):
            print(f"  ... {i}/{len(event_slugs)}")
        markets = fetch_event_markets(ev_slug)
        for m in markets:
            mslug = (m.get("slug") or "").strip()
            if not mslug or mslug in seen_market:
                continue
            seen_market.add(mslug)
            q = (m.get("question") or "").replace("\t", " ").replace("\n", " ")
            gst = m.get("gameStartTime") or ""
            lines_tsv.append(f"{ev_slug}\t{mslug}\t{q}\t{gst}")
            slug_only.append(mslug)
    out_tsv.write_text("\n".join(lines_tsv) + "\n", encoding="utf-8")
    out_slugs.write_text("\n".join(slug_only) + "\n", encoding="utf-8")
    print()
    print(f"Wrote {len(seen_market)} market row(s) to:")
    print(f"  {out_tsv}")
    print(f"  {out_slugs}")
    print()
    print("Use each line of the .txt as the slug in /add_position <slug> <YES/NO> <price>,")
    print("or paste into bulk_add (one '<slug> YES 0.50' line per market — set your own prices).")


def fetch_orderbook(token_id: str) -> Optional[dict]:
    """Fetch orderbook for a given token_id from CLOB API."""
    try:
        base = "https://clob.polymarket.com/book"
        query = urllib.parse.urlencode({"token_id": token_id})
        url = f"{base}?{query}"
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "LPScan/1.0 (LP rewards analyzer)"},
        )
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        print(f"Failed to fetch orderbook for token {token_id}: {e}", file=sys.stderr)
        return None


def filter_reward_markets(markets):
    """Keep only markets with clobRewards and rewardsDailyRate > 0."""
    return [
        m
        for m in markets
        if m.get("clobRewards")
        and len(m["clobRewards"]) > 0
        and float(m["clobRewards"][0].get("rewardsDailyRate", 0)) > 0
    ]


def classify_event_type(question: str) -> dict:
    """Classify event type for spike risk (0–100)."""
    q = (question or "").lower()
    binary_triggers = [
        "resign", "resigns", "out as", "step down", "fired", "removed",
        "strike", "strikes", "attack", "invade", "invasion", "war",
        "die", "dies", "death", "assassin",
        "announce", "announcement", "declare",
        "shut down", "shutdown", "default",
        "ceasefire", "peace deal", "treaty",
    ]
    scheduled_triggers = [
        "fed ", "fomc", "interest rate", "rate cut", "rate hike",
        "election", "vote", "referendum",
        "nominee", "nomination", "primary", "democratic nominee",
        "republican nominee", "general election",
        "super bowl", "world cup", "championship", "finals",
        "earnings", "quarterly", "q1", "q2", "q3", "q4",
        "meeting", "summit", "conference",
    ]
    # Congressional district (PA-03, FL-19, etc.) = scheduled primary/nomination
    district_pattern = re.compile(r"\b[A-Z]{2}-\d{1,2}\b")
    # Asset price markets: EXCLUDE from low-risk LP (one pump/dump can move price violently)
    asset_price_triggers = [
        "bitcoin", "btc", "eth", "crypto", "price above", "price below",
        "stock", "s&p", "nasdaq", "dow", "spx", "sp500",
        "silver", "gold", " hit ", " above $", " below $",
        "close over", "close above", "close below",
        " (si)", " (gc)", "gc)", "si)",
    ]
    gradual_triggers = [
        "gdp", "inflation", "unemployment",
        "subscribers", "followers", "views", "streams",
        "before gta", "by end of year", "by 2027", "by 2028",
    ]
    is_binary = any(t in q for t in binary_triggers)
    is_scheduled = any(t in q for t in scheduled_triggers)
    if district_pattern.search(question or ""):
        is_scheduled = True
    is_asset_price = any(t in q for t in asset_price_triggers)
    is_gradual = any(t in q for t in gradual_triggers)
    if is_asset_price:
        base_spike_risk = 72  # One pump/dump can change things — exclude from low-risk LP
    elif is_binary:
        base_spike_risk = 85
    elif is_scheduled:
        base_spike_risk = 65
    elif is_gradual:
        base_spike_risk = 25
    else:
        base_spike_risk = 50
    category = (
        "asset_price"
        if is_asset_price
        else "binary"
        if is_binary
        else "scheduled"
        if is_scheduled
        else "gradual"
        if is_gradual
        else "unknown"
    )
    return {
        "spike_risk": base_spike_risk,
        "is_binary": is_binary,
        "is_scheduled": is_scheduled,
        "is_gradual": is_gradual,
        "is_asset_price": is_asset_price,
        "category": category,
    }


def calculate_time_proximity_risk(
    end_date_str: str, known_spike_date_str: Optional[str] = None
) -> int:
    """Time-to-resolution risk 0–100 (exponential). Uses nearer of end_date vs known_spike_date."""
    now = datetime.now(timezone.utc)
    candidates = []
    for s in (end_date_str, known_spike_date_str):
        if not s:
            continue
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
            candidates.append((d - now).total_seconds() / 3600)
        except Exception:
            continue
    if not candidates:
        return 40
    hours_remaining = min(candidates)
    if hours_remaining < 0:
        return 100
    if hours_remaining < 6:
        return 98
    if hours_remaining < 24:
        return 90
    if hours_remaining < 72:
        return 75
    if hours_remaining < 168:
        return 55
    if hours_remaining < 720:
        return 35
    if hours_remaining < 2160:
        return 20
    return 8


def calculate_adverse_selection_risk(market: dict) -> float:
    """Adverse selection risk 0–100."""
    outcome_prices = market.get("outcomePrices", '["0.5", "0.5"]')
    try:
        if isinstance(outcome_prices, str):
            prices = json.loads(outcome_prices.replace("'", '"'))
        else:
            prices = outcome_prices
        yes_price = float(prices[0]) if prices else 0.5
    except Exception:
        yes_price = 0.5
    price_distance = abs(yes_price - 0.50)
    extremity_risk = price_distance * 80
    liquidity = float(market.get("liquidity", 0) or 0)
    if liquidity < 10000:
        liquidity_risk = 30
    elif liquidity < 50000:
        liquidity_risk = 20
    elif liquidity < 200000:
        liquidity_risk = 10
    else:
        liquidity_risk = 5
    competitive = float(market.get("competitive", 0) or 0)
    competition_risk = (1 - competitive) * 30
    return min(extremity_risk + liquidity_risk + competition_risk, 100)


def calculate_risk_score(market: dict) -> dict:
    """Composite risk: 50% spike + 30% time + 20% adverse selection."""
    question = market.get("question", "")
    event_analysis = classify_event_type(question)
    spike_risk = event_analysis["spike_risk"]
    time_risk = calculate_time_proximity_risk(
        market.get("endDate"), market.get("knownSpikeDate")
    )
    adverse_risk = calculate_adverse_selection_risk(market)
    if event_analysis["is_binary"] and time_risk > 70:
        spike_risk = min(spike_risk * 1.15, 100)
    composite = (spike_risk * 0.50) + (time_risk * 0.30) + (adverse_risk * 0.20)
    return {
        "composite": round(composite, 1),
        "spike_risk": round(spike_risk, 1),
        "time_risk": time_risk,
        "adverse_selection_risk": round(adverse_risk, 1),
        "event_category": event_analysis["category"],
        "is_binary_event": event_analysis["is_binary"],
    }


def calculate_days_remaining(end_date_str):
    """Days until market resolution."""
    if not end_date_str:
        return 365
    try:
        end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        days = (end_date - now).days
        return max(days, 0)
    except Exception:
        return 365


def format_end_date(end_date_str) -> str:
    """Human-readable resolution date, e.g. 'December 31, 2026'."""
    if not end_date_str:
        return "unknown"
    try:
        end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
        return end_date.strftime("%B %d, %Y")
    except Exception:
        return "unknown"


def format_reasoning(row: dict) -> str:
    """Short reasoning paragraph from available data."""
    parts = []
    # Resolution and farming window
    days = row["days_remaining"]
    end_readable = row.get("end_date_readable", "unknown")
    parts.append(
        f"This market resolves on {end_readable}, leaving ~{days} days to farm LP rewards."
    )
    # Liquidity / volume context
    vol = row.get("volume") or 0
    liq = row.get("liquidity") or 0
    if vol < 50_000 and liq < 20_000:
        parts.append("Low total volume and liquidity — consider sizing down or monitoring spread.")
    elif vol < 200_000:
        parts.append("Moderate volume; liquidity is adequate but not deep.")
    else:
        parts.append("Solid volume and liquidity for the size of the market.")
    # Risk nuance by category
    cat = row.get("event_category", "unknown")
    if cat == "scheduled":
        parts.append("Risk is scheduled: there is a known window when the outcome can move sharply.")
    elif cat == "binary":
        parts.append("Binary-style event — a single headline could move the market sharply; keep position size in check.")
    elif cat == "gradual":
        parts.append("Gradual-type event; probability tends to move incrementally rather than in one spike.")
    else:
        parts.append("Event type is generic; monitor for news that could create a sudden move.")
    # Optional: movie/entertainment nuance
    q = (row.get("question") or "").lower()
    if "opening weekend" in q or "box office" in q or "top grossing" in q or "movie" in q or "film" in q:
        parts.append("Performance of related releases through the year may move the probability; no fixed release calendar is applied here.")
    return " ".join(parts)


def calculate_capital_efficiency(market: dict) -> float:
    """Daily rewards per dollar of estimated min capital."""
    rewards = market["clobRewards"][0]
    daily_rate = float(rewards.get("rewardsDailyRate", 0))
    liquidity = float(market.get("liquidity", 0) or 0)
    min_capital = max(liquidity * 0.01, 100)
    if min_capital == 0:
        return 0.0
    return round(daily_rate / min_capital, 4)


def get_current_prices(market: dict) -> tuple[float, float]:
    """Return (yes_price, no_price) from outcomePrices."""
    outcome_prices = market.get("outcomePrices", '["0.5", "0.5"]')
    try:
        if isinstance(outcome_prices, str):
            prices = json.loads(outcome_prices.replace("'", '"'))
        else:
            prices = outcome_prices
        yes_price = float(prices[0]) if prices else 0.5
        no_price = float(prices[1]) if len(prices) > 1 else 1.0 - yes_price
    except Exception:
        yes_price, no_price = 0.5, 0.5
    return yes_price, no_price


def parse_token_ids(market: dict) -> tuple[Optional[str], Optional[str]]:
    """Extract YES and NO token IDs from market."""
    try:
        token_ids = json.loads(market.get("clobTokenIds", "[]"))
        if isinstance(token_ids, list) and len(token_ids) >= 2:
            return str(token_ids[0]), str(token_ids[1])
    except Exception:
        pass
    return None, None


def build_market_row(market: dict) -> dict | None:
    """Build a single enriched row; None if missing data."""
    if not market.get("clobRewards") or not market["clobRewards"]:
        return None
    rewards = market["clobRewards"][0]
    daily_rate = float(rewards.get("rewardsDailyRate", 0))
    if daily_rate <= 0:
        return None
    risk = calculate_risk_score(market)
    days_remaining = calculate_days_remaining(market.get("endDate"))
    liquidity = float(market.get("liquidity", 0) or 0)
    min_capital = max(liquidity * 0.01, 100)
    apy = (daily_rate / min_capital) * 365 * 100 if min_capital else 0
    outcome_prices = market.get("outcomePrices", "[\"0.5\", \"0.5\"]")
    try:
        prices = json.loads(outcome_prices.replace("'", '"')) if isinstance(outcome_prices, str) else outcome_prices
        yes_price = float(prices[0]) if prices else 0.5
    except Exception:
        yes_price = 0.5
    slug = market.get("slug", "")
    volume = float(market.get("volume", 0) or 0)
    end_date_str = market.get("endDate") or ""
    return {
        "question": (market.get("question") or "")[:70],
        "slug": slug,
        "daily_rewards": round(daily_rate, 2),
        "days_remaining": days_remaining,
        "min_capital_estimate": round(min_capital, 2),
        "liquidity": round(liquidity, 2),
        "volume": round(volume, 2),
        "end_date_readable": format_end_date(end_date_str),
        "spread_cents": round(float(market.get("spread", 0.05) or 0.05) * 100, 2),
        "yes_price": yes_price,
        "risk_composite": risk["composite"],
        "risk_spike": risk["spike_risk"],
        "risk_time": risk["time_risk"],
        "risk_adverse": risk["adverse_selection_risk"],
        "event_category": risk["event_category"],
        "capital_efficiency": calculate_capital_efficiency(market),
        "estimated_apy": round(apy, 2),
        "url": f"https://polymarket.com/event/{slug}" if slug else "",
    }


def risk_label(score: float) -> str:
    """Human-readable risk label."""
    if score <= 25:
        return "Low"
    if score <= 45:
        return "Moderate"
    if score <= 65:
        return "Elevated"
    if score <= 80:
        return "High"
    return "Extreme"


def colored_risk_label(score: float) -> str:
    """Color-coded risk label for terminal output."""
    label = risk_label(score)
    if score <= 25:
        return color_text(label, GREEN)
    if score <= 45:
        return color_text(label, YELLOW)
    return color_text(label, RED)


def load_saved_positions() -> list[Position]:
    """Load positions from positions.json if it exists."""
    if not POSITIONS_PATH.exists():
        return []
    try:
        with POSITIONS_PATH.open("r", encoding="utf-8") as f:
            raw = json.load(f)
        positions: list[Position] = []
        for item in raw:
            try:
                positions.append(
                    Position(
                        market_slug=item["market_slug"],
                        side=item["side"],
                        my_limit_price=float(item["my_limit_price"]),
                        notes=item.get("notes", ""),
                    )
                )
            except Exception:
                continue
        return positions
    except Exception as e:
        print(f"Failed to load positions from {POSITIONS_PATH}: {e}", file=sys.stderr)
        return []


def save_positions(positions: list[Position]) -> None:
    """Persist positions to positions.json."""
    try:
        data = [
            {
                "market_slug": p.market_slug,
                "side": p.side,
                "my_limit_price": p.my_limit_price,
                "notes": p.notes,
            }
            for p in positions
        ]
        with POSITIONS_PATH.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"Failed to save positions to {POSITIONS_PATH}: {e}", file=sys.stderr)


def find_position_index(
    positions: list[Position], slug: str, side: str
) -> Optional[int]:
    """Return index of existing position with same normalized slug + side, or None."""
    norm_slug = normalize_market_slug(slug)
    side = side.upper()
    for i, p in enumerate(positions):
        if p.side.upper() != side:
            continue
        if normalize_market_slug(p.market_slug) == norm_slug:
            return i
    return None


def parse_bulk_positions(
    text: str, positions: list[Position]
) -> tuple[int, int, int]:
    """
    Parse bulk positions from multi-line text.
    Each non-empty line should be:
      <slug-or-url> <YES/NO> <price>
    Returns (added_count, skipped_malformed, updated_count).
    """
    added = 0
    skipped = 0
    updated = 0
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 3:
            skipped += 1
            continue
        slug = parts[0]
        side = parts[1].upper()
        try:
            price = float(parts[2])
        except ValueError:
            skipped += 1
            continue
        if side not in {"YES", "NO"}:
            skipped += 1
            continue
        # Update existing position if found, otherwise add new
        existing_idx = find_position_index(positions, slug, side)
        if existing_idx is not None:
            positions[existing_idx].my_limit_price = price
            updated += 1
        else:
            positions.append(
                Position(market_slug=slug, side=side, my_limit_price=price, notes="")
            )
            added += 1
    if added or updated:
        save_positions(positions)
    return added, skipped, updated


def prompt_for_positions() -> list[Position]:
    """Interactively collect LP positions from the user."""
    print()
    print("Enter your LP positions (leave market slug empty to finish).")
    print(
        "  Tip: Press Enter at the first prompt to skip — you can add positions via "
        "Telegram (/list_event, /add_position) once the monitor is running."
    )
    positions: list[Position] = []
    while True:
        slug = input("  Market slug (blank to finish): ").strip()
        if not slug:
            break
        side = input("  Side [YES/NO]: ").strip().upper()
        if side not in {"YES", "NO"}:
            print("    Invalid side, must be YES or NO. Skipping.")
            continue
        existing_idx = find_position_index(positions, slug, side)
        if existing_idx is not None:
            existing = positions[existing_idx]
            print(
                f"    You already have a position on this market/side "
                f"({existing.side} @ {existing.my_limit_price:.3f})."
            )
            choice = (
                input("    Update this position's price instead? [Y/n]: ")
                .strip()
                .lower()
                or "y"
            )
            if choice != "y":
                print("    Keeping existing position, skipping new one.\n")
                continue
        try:
            limit_str = input("  Your limit price (e.g. 0.36): ").strip()
            my_limit = float(limit_str)
        except ValueError:
            print("    Invalid price. Skipping this position.")
            continue
        if existing_idx is not None:
            positions[existing_idx].my_limit_price = my_limit
            print("  Position updated.\n")
        else:
            positions.append(
                Position(
                    market_slug=slug,
                    side=side,
                    my_limit_price=my_limit,
                    notes="",
                )
            )
            print("  Position added.\n")
    return positions


def get_positions_with_persistence() -> list[Position]:
    """Load saved positions, optionally extend/edit via interactive input, and save."""
    positions = load_saved_positions()
    if positions:
        print()
        print(f"Found {len(positions)} saved positions in {POSITIONS_PATH.name}.")
        use_saved = input("Use these saved positions? [Y/n]: ").strip().lower() or "y"
        if use_saved == "y":
            # Show current positions with indices so the user can clean them up
            print()
            print("Current saved positions:")
            for idx, p in enumerate(positions, 1):
                print(f"  {idx}. {p.side} @ {p.my_limit_price:.3f} on {p.market_slug}")
            print()
            to_remove = (
                input(
                    "Enter indices to remove (space-separated), or press Enter to keep all: "
                )
                .strip()
            )
            if to_remove:
                try:
                    idx_values = sorted(
                        {
                            int(tok)
                            for tok in to_remove.split()
                            if tok.strip()
                        },
                        reverse=True,
                    )
                except ValueError:
                    print("Invalid indices entered; skipping removal step.")
                else:
                    max_idx = len(positions)
                    removed_any = False
                    for i in idx_values:
                        if 1 <= i <= max_idx:
                            removed = positions.pop(i - 1)
                            print(
                                f"  Removed {i}. {removed.side} @ {removed.my_limit_price:.3f} on {removed.market_slug}"
                            )
                            removed_any = True
                        else:
                            print(f"  Index {i} out of range; ignoring.")
                    if removed_any:
                        print()
                        print(f"{len(positions)} position(s) remain after removal.")

            add_more = input("Add more positions now? [y/N]: ").strip().lower() or "n"
            if add_more == "y":
                extra = prompt_for_positions()
                positions.extend(extra)
        else:
            print("Discarding saved positions for this run; enter new ones.")
            print(
                "  (Or press Enter at the first 'Market slug' prompt to skip and use Telegram only.)"
            )
            positions = prompt_for_positions()
    else:
        positions = prompt_for_positions()

    if positions:
        save_positions(positions)
    return positions


def prompt_for_telegram_bot() -> Optional[TelegramBot]:
    """Ask user for Telegram bot token and chat id."""
    print()
    print("Telegram alerts setup (low bid depth: USD at or above your limit).")
    token = input("  Telegram bot token (blank to disable alerts): ").strip()
    if not token:
        print("  Telegram alerts disabled.")
        return None
    chat_id = input("  Telegram chat_id (user or group id): ").strip()
    if not chat_id:
        print("  chat_id missing, Telegram alerts disabled.")
        return None
    print("  Telegram alerts enabled.")
    return TelegramBot(token=token, chat_id=chat_id)


def load_monitor_config() -> Optional[dict]:
    """Load Telegram + monitor settings from monitor_config.json."""
    if not MONITOR_CONFIG_PATH.exists():
        return None
    try:
        with MONITOR_CONFIG_PATH.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"Failed to load monitor config from {MONITOR_CONFIG_PATH}: {e}", file=sys.stderr)
        return None


def save_monitor_config(config: dict) -> None:
    """Persist Telegram + monitor settings."""
    try:
        with MONITOR_CONFIG_PATH.open("w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
    except Exception as e:
        print(f"Failed to save monitor config to {MONITOR_CONFIG_PATH}: {e}", file=sys.stderr)


def send_telegram_test() -> int:
    """Send a one-off test message using monitor_config.json (no interactive prompts)."""
    if not MONITOR_CONFIG_PATH.exists():
        print(
            f"Missing {MONITOR_CONFIG_PATH.name} — copy monitor_config.example.json "
            f"and add telegram.bot_token + telegram.chat_id.",
            file=sys.stderr,
        )
        return 1
    config = load_monitor_config()
    if not config:
        return 1
    tg = config.get("telegram", {}) or {}
    token = (tg.get("bot_token") or "").strip()
    chat_id = str(tg.get("chat_id") or "").strip()
    if not token or not chat_id:
        print(
            "monitor_config.json must include telegram.bot_token and telegram.chat_id.",
            file=sys.stderr,
        )
        return 1
    bot = TelegramBot(token=token, chat_id=chat_id)
    ok = bot.send_message(
        "<b>LPWatch test</b>\n\n"
        "If you see this, the bot reached your Telegram chat. "
        "Whether your PC shows a banner depends on "
        "<b>Telegram Desktop → Settings → Notifications</b> "
        "and macOS <b>System Settings → Notifications → Telegram</b>.",
        parse_mode="HTML",
    )
    if ok:
        print("Test message sent. Check Telegram on this device.")
        return 0
    return 1


def get_monitor_config_with_persistence() -> tuple[
    Optional[TelegramBot], int, float, str, bool
]:
    """
    Load saved Telegram/settings config if available, optionally override via prompts,
    and persist latest settings.
    Returns (TelegramBot|None, poll_interval_seconds, min_bid_depth_usd,
             wallet_address, sync_positions_from_wallet).
    """
    config = load_monitor_config()
    if config:
        print()
        print(f"Found saved monitor config in {MONITOR_CONFIG_PATH.name}.")
        use_saved = input("Use saved Telegram/settings? [Y/n]: ").strip().lower() or "y"
        if use_saved == "y":
            tg_cfg = config.get("telegram", {}) or {}
            token = tg_cfg.get("bot_token", "").strip()
            chat_id = str(tg_cfg.get("chat_id", "")).strip()
            bot = TelegramBot(token=token, chat_id=chat_id) if token and chat_id else None
            settings = config.get("settings", {}) or {}
            poll_interval = int(settings.get("poll_interval_seconds", 25))
            min_bid_depth = float(settings.get("min_bid_depth_usd", 50000.0))
            wallet_address = str(settings.get("wallet_address", "") or "").strip()
            sync_wallet = bool(settings.get("sync_positions_from_wallet", False))
            return bot, poll_interval, min_bid_depth, wallet_address, sync_wallet
        else:
            print("Discarding saved monitor config for this run; enter new settings.")

    # Fresh prompts
    bot = prompt_for_telegram_bot()
    try:
        poll_str = input("Poll interval seconds [default 25]: ").strip()
        poll_interval = int(poll_str) if poll_str else 25
    except ValueError:
        poll_interval = 25
    try:
        depth_str = input(
            "Min bid depth alert (USD, bids at or above your limit) [default 50000]: "
        ).strip()
        min_bid_depth = float(depth_str) if depth_str else 50000.0
    except ValueError:
        min_bid_depth = 50000.0

    wallet_in = input(
        "Polymarket wallet address (0x...) for auto-sync, or blank to skip: "
    ).strip()
    sync_wallet = False
    if wallet_in:
        sync_ans = (
            input(
                "Refresh positions every poll from this wallet via Data API? [y/N]: "
            )
            .strip()
            .lower()
        )
        sync_wallet = sync_ans == "y"

    # Save for next time
    cfg = {
        "telegram": {},
        "settings": {
            "poll_interval_seconds": poll_interval,
            "min_bid_depth_usd": min_bid_depth,
            "wallet_address": wallet_in,
            "sync_positions_from_wallet": sync_wallet,
        },
    }
    if bot is not None:
        cfg["telegram"] = {"bot_token": bot.token, "chat_id": bot.chat_id}
    save_monitor_config(cfg)
    return bot, poll_interval, min_bid_depth, wallet_in, sync_wallet


def process_telegram_commands(
    bot: Optional[TelegramBot],
    positions: list[Position],
    last_update_id: Optional[int],
) -> Optional[int]:
    """Handle simple Telegram commands to manage positions.

    Supported commands:
    - /positions
    - /out_of_range
    - /market <slug-or-url>
    - /add_position <slug> <YES/NO> <limit_price> [notes...]
    - /remove_position <index>
    - /help
    """
    if bot is None:
        return last_update_id

    next_offset = (last_update_id + 1) if last_update_id is not None else None
    updates = bot.get_updates(offset=next_offset, timeout=0)
    if not updates:
        return last_update_id

    for upd in updates:
        upd_id = upd.get("update_id")
        if upd_id is not None:
            last_update_id = upd_id
        msg = upd.get("message") or upd.get("edited_message")
        if not msg:
            continue
        chat = msg.get("chat") or {}
        chat_id = str(chat.get("id", ""))
        # Only respond in the configured chat
        if bot.chat_id and chat_id != bot.chat_id:
            continue
        text = (msg.get("text") or "").strip()

        # If we're expecting bulk input from this chat, treat the next non-command
        # text message as bulk positions payload.
        if BULK_INPUT_PENDING.get(chat_id) and text and not text.startswith("/"):
            added, skipped, updated = parse_bulk_positions(text, positions)
            BULK_INPUT_PENDING.pop(chat_id, None)
            msg = f"Bulk add complete. Added {added} position(s)"
            if updated:
                msg += f", updated {updated} existing position(s)"
            if skipped:
                msg += f", skipped {skipped} malformed line(s)"
            msg += "."
            bot.send_message(msg)
            continue

        if not text.startswith("/"):
            continue

        parts = text.split()
        cmd = parts[0].lower()
        # Strip optional @botname suffix (Telegram may send /cmd@bot in some clients)
        if "@" in cmd:
            cmd = cmd.split("@", 1)[0]

        if cmd in {"/positions", "/pos"}:
            if not positions:
                bot.send_message("No positions saved.")
            else:
                # Build enriched view: question, side, current, limit, distance, bids_before
                rows: list[dict] = []
                # Cache orderbooks within this /positions call
                orderbook_cache: dict[str, Optional[dict]] = {}
                for idx, p in enumerate(positions, 1):
                    market = fetch_market_by_slug(p.market_slug)
                    if not market:
                        rows.append(
                            {
                                "idx": idx,
                                "question": p.market_slug,
                                "side": p.side,
                                "current_price": None,
                                "limit_price": p.my_limit_price,
                                "distance_cents": None,
                                "bids_before": None,
                                "game_start": None,
                            }
                        )
                        continue
                    yes_price, no_price = get_current_prices(market)
                    current_price = yes_price if p.side == "YES" else no_price
                    distance_cents = abs(current_price - p.my_limit_price) * 100
                    yes_token_id, no_token_id = parse_token_ids(market)
                    token_id = yes_token_id if p.side == "YES" else no_token_id
                    bids_dollars_before = 0.0
                    if token_id:
                        if token_id not in orderbook_cache:
                            orderbook_cache[token_id] = fetch_orderbook(token_id)
                        ob = orderbook_cache.get(token_id) or {}
                        bids = ob.get("bids", []) or []
                        for b in bids:
                            try:
                                price = float(b.get("price", 0))
                                size = float(
                                    b.get("quantity")
                                    or b.get("size")
                                    or b.get("remaining")
                                    or 0
                                )
                            except Exception:
                                continue
                            if price >= p.my_limit_price:
                                bids_dollars_before += price * size
                    rows.append(
                        {
                            "idx": idx,
                            "question": market.get("question") or p.market_slug,
                            "side": p.side,
                            "current_price": current_price,
                            "limit_price": p.my_limit_price,
                            "distance_cents": distance_cents,
                            "bids_before": bids_dollars_before,
                            "game_start": parse_market_game_start_utc(market),
                        }
                    )

                now_tg = datetime.now(timezone.utc)
                rows.sort(key=lambda r: position_row_sort_key(r, now_tg))

                # Build message chunks under Telegram limit
                header = "<b>Current positions</b>\n(sorted by soonest game first, then distance):"
                current_block = header
                chunks: list[str] = []
                for r in rows:
                    idx = r["idx"]
                    q = r["question"]
                    if len(q) > 120:
                        q = q[:117] + "..."
                    cp = r["current_price"]
                    lp = r["limit_price"]
                    dist = r["distance_cents"]
                    bids = r["bids_before"]
                    if cp is None or dist is None or bids is None:
                        line = (
                            f"\n\n<b>{idx}. {q}</b>\n"
                            f"Side: <b>{r['side']}</b> • "
                            f"Limit: <b>{lp:.3f}</b> • "
                            "Current: <b>n/a</b> • "
                            "Distance: <b>n/a</b> • "
                            "Bids before: <b>n/a</b>"
                        )
                    else:
                        # Distance label same as terminal thresholds (WITHOUT colors)
                        if dist <= 1.0:
                            dist_str = f"{dist:.1f}¢"
                        elif dist <= 2.0:
                            dist_str = f"{dist:.1f}¢"
                        elif dist >= 5.0:
                            dist_str = f"{dist:.1f}¢ OUT OF RANGE"
                        else:
                            dist_str = f"{dist:.1f}¢"
                        line = (
                            f"\n\n<b>{idx}. {q}</b>\n"
                            f"Side: <b>{r['side']}</b> • "
                            f"Current: <b>{cp:.3f}</b> • "
                            f"Limit: <b>{lp:.3f}</b> • "
                            f"Distance: <b>{dist_str}</b> • "
                            f"Bids before: {format_bids_before_telegram_html(bids)}"
                        )

                    if len(current_block) + len(line) > 3500:
                        chunks.append(current_block)
                        current_block = header + line
                    else:
                        current_block += line

                if current_block:
                    chunks.append(current_block)

                for chunk in chunks:
                    bot.send_message(chunk, parse_mode="HTML")

        elif cmd == "/out_of_range":
            """List only OUT OF RANGE positions (distance >= 5¢)."""
            if not positions:
                bot.send_message("No positions saved.")
            else:
                rows: list[dict] = []
                orderbook_cache: dict[str, Optional[dict]] = {}
                for idx, p in enumerate(positions, 1):
                    market = fetch_market_by_slug(p.market_slug)
                    if not market:
                        continue
                    yes_price, no_price = get_current_prices(market)
                    current_price = yes_price if p.side == "YES" else no_price
                    distance_cents = abs(current_price - p.my_limit_price) * 100
                    if distance_cents < 5.0:
                        continue
                    yes_token_id, no_token_id = parse_token_ids(market)
                    token_id = yes_token_id if p.side == "YES" else no_token_id
                    bids_dollars_before = 0.0
                    if token_id:
                        if token_id not in orderbook_cache:
                            orderbook_cache[token_id] = fetch_orderbook(token_id)
                        ob = orderbook_cache.get(token_id) or {}
                        bids = ob.get("bids", []) or []
                        for b in bids:
                            try:
                                price = float(b.get("price", 0))
                                size = float(
                                    b.get("quantity")
                                    or b.get("size")
                                    or b.get("remaining")
                                    or 0
                                )
                            except Exception:
                                continue
                            if price >= p.my_limit_price:
                                bids_dollars_before += price * size
                    rows.append(
                        {
                            "idx": idx,
                            "question": market.get("question") or p.market_slug,
                            "side": p.side,
                            "current_price": current_price,
                            "limit_price": p.my_limit_price,
                            "distance_cents": distance_cents,
                            "bids_before": bids_dollars_before,
                            "game_start": parse_market_game_start_utc(market),
                        }
                    )

                if not rows:
                    bot.send_message("No OUT OF RANGE positions (distance ≥ 5¢).")
                else:
                    now_tg = datetime.now(timezone.utc)
                    rows.sort(key=lambda r: position_row_sort_key(r, now_tg))
                    header = "<b>OUT OF RANGE positions</b>\n(distance ≥ 5¢; soonest game first):"
                    current_block = header
                    chunks: list[str] = []
                    for r in rows:
                        idx = r["idx"]
                        q = r["question"]
                        if len(q) > 120:
                            q = q[:117] + "..."
                        cp = r["current_price"]
                        lp = r["limit_price"]
                        dist = r["distance_cents"]
                        bids = r["bids_before"]
                        dist_str = f"{dist:.1f}¢ OUT OF RANGE"
                        line = (
                            f"\n\n<b>{idx}. {q}</b>\n"
                            f"Side: <b>{r['side']}</b> • "
                            f"Current: <b>{cp:.3f}</b> • "
                            f"Limit: <b>{lp:.3f}</b> • "
                            f"Distance: <b>{dist_str}</b> • "
                            f"Bids before: {format_bids_before_telegram_html(bids)}"
                        )
                        if len(current_block) + len(line) > 3500:
                            chunks.append(current_block)
                            current_block = header + line
                        else:
                            current_block += line
                    if current_block:
                        chunks.append(current_block)
                    for chunk in chunks:
                        bot.send_message(chunk, parse_mode="HTML")

        elif cmd == "/market" and len(parts) >= 2:
            """Show positions for a specific market (by slug or URL)."""
            if not positions:
                bot.send_message("No positions saved.")
            else:
                target = normalize_market_slug(parts[1])
                rows: list[dict] = []
                orderbook_cache: dict[str, Optional[dict]] = {}
                for idx, p in enumerate(positions, 1):
                    if normalize_market_slug(p.market_slug) != target:
                        continue
                    market = fetch_market_by_slug(p.market_slug)
                    if not market:
                        continue
                    yes_price, no_price = get_current_prices(market)
                    current_price = yes_price if p.side == "YES" else no_price
                    distance_cents = abs(current_price - p.my_limit_price) * 100
                    yes_token_id, no_token_id = parse_token_ids(market)
                    token_id = yes_token_id if p.side == "YES" else no_token_id
                    bids_dollars_before = 0.0
                    if token_id:
                        if token_id not in orderbook_cache:
                            orderbook_cache[token_id] = fetch_orderbook(token_id)
                        ob = orderbook_cache.get(token_id) or {}
                        bids = ob.get("bids", []) or []
                        for b in bids:
                            try:
                                price = float(b.get("price", 0))
                                size = float(
                                    b.get("quantity")
                                    or b.get("size")
                                    or b.get("remaining")
                                    or 0
                                )
                            except Exception:
                                continue
                            if price >= p.my_limit_price:
                                bids_dollars_before += price * size
                    rows.append(
                        {
                            "idx": idx,
                            "question": market.get("question") or p.market_slug,
                            "side": p.side,
                            "current_price": current_price,
                            "limit_price": p.my_limit_price,
                            "distance_cents": distance_cents,
                            "bids_before": bids_dollars_before,
                            "game_start": parse_market_game_start_utc(market),
                        }
                    )

                if not rows:
                    bot.send_message(
                        "No positions found for that market. "
                        "Make sure you used the slug or URL of a market you have saved."
                    )
                else:
                    now_tg = datetime.now(timezone.utc)
                    rows.sort(key=lambda r: position_row_sort_key(r, now_tg))
                    # Use the first row's question as market title
                    title = rows[0]["question"]
                    if len(title) > 120:
                        title = title[:117] + "..."
                    header = (
                        "<b>Positions for market</b>\n"
                        f"{title}\n"
                        "(sorted by soonest game first):"
                    )
                    current_block = header
                    chunks: list[str] = []
                    for r in rows:
                        idx = r["idx"]
                        q = r["question"]
                        if len(q) > 120:
                            q = q[:117] + "..."
                        cp = r["current_price"]
                        lp = r["limit_price"]
                        dist = r["distance_cents"]
                        bids = r["bids_before"]
                        if dist <= 1.0:
                            dist_str = f"{dist:.1f}¢"
                        elif dist <= 2.0:
                            dist_str = f"{dist:.1f}¢"
                        elif dist >= 5.0:
                            dist_str = f"{dist:.1f}¢ OUT OF RANGE"
                        else:
                            dist_str = f"{dist:.1f}¢"
                        line = (
                            f"\n\n<b>{idx}. {q}</b>\n"
                            f"Side: <b>{r['side']}</b> • "
                            f"Current: <b>{cp:.3f}</b> • "
                            f"Limit: <b>{lp:.3f}</b> • "
                            f"Distance: <b>{dist_str}</b> • "
                            f"Bids before: {format_bids_before_telegram_html(bids)}"
                        )
                        if len(current_block) + len(line) > 3500:
                            chunks.append(current_block)
                            current_block = header + line
                        else:
                            current_block += line
                    if current_block:
                        chunks.append(current_block)
                    for chunk in chunks:
                        bot.send_message(chunk, parse_mode="HTML")

        elif cmd == "/add_position" and len(parts) >= 4:
            slug = parts[1]
            side = parts[2].upper()
            try:
                limit_price = float(parts[3])
            except ValueError:
                bot.send_message("Invalid price. Usage: /add_position <slug> <YES/NO> <price> [notes]")
                continue
            if side not in {"YES", "NO"}:
                bot.send_message("Side must be YES or NO. Usage: /add_position <slug> <YES/NO> <price>")
                continue
            existing_idx = find_position_index(positions, slug, side)
            if existing_idx is not None:
                p = positions[existing_idx]
                old_price = p.my_limit_price
                p.my_limit_price = limit_price
                save_positions(positions)
                bot.send_message(
                    "Updated existing position on this market/side.\n"
                    f"{p.side} on {p.market_slug}\n"
                    f"Old price: {old_price:.3f}\n"
                    f"New price: {limit_price:.3f}"
                )
            else:
                positions.append(
                    Position(
                        market_slug=slug,
                        side=side,
                        my_limit_price=limit_price,
                        notes="",
                    )
                )
                save_positions(positions)
                bot.send_message(f"Added position: {side} @ {limit_price:.3f} on {slug}")

        elif cmd == "/edit_position" and len(parts) >= 3:
            try:
                idx = int(parts[1])
            except ValueError:
                bot.send_message("Index must be a number. Usage: /edit_position <index> <new_price>")
                continue
            if not (1 <= idx <= len(positions)):
                bot.send_message(
                    f"Index out of range. You currently have {len(positions)} "
                    f"position{'s' if len(positions) != 1 else ''}. "
                    "Use /positions to see valid indices."
                )
                continue
            try:
                new_price = float(parts[2])
            except ValueError:
                bot.send_message("Invalid price. Usage: /edit_position <index> <new_price>")
                continue
            p = positions[idx - 1]
            old_price = p.my_limit_price
            p.my_limit_price = new_price
            save_positions(positions)
            bot.send_message(
                f"Updated position {idx}: {p.side} on {p.market_slug}\n"
                f"Old price: {old_price:.3f}\n"
                f"New price: {new_price:.3f}"
            )

        elif cmd == "/bulk_add":
            BULK_INPUT_PENDING[chat_id] = True
            bot.send_message(
                "Send positions in the next message, one per line, in this format:\n"
                "<slug-or-url> <YES/NO> <price>\n\n"
                "Example:\n"
                "https://polymarket.com/event/.../market1 YES 0.75\n"
                "https://polymarket.com/event/.../market2 NO 0.43"
            )

        elif cmd == "/remove_position" and len(parts) >= 2:
            # Support bulk remove: /remove_position 1 2 3
            idx_tokens = parts[1:]
            idx_values: list[int] = []
            invalid_tokens: list[str] = []
            for tok in idx_tokens:
                try:
                    idx_values.append(int(tok))
                except ValueError:
                    invalid_tokens.append(tok)
            if not idx_values:
                bot.send_message(
                    "No valid indices provided. Usage: /remove_position <index> [index2 index3 ...]"
                )
                continue
            # Validate ranges
            max_idx = len(positions)
            out_of_range = [i for i in idx_values if not (1 <= i <= max_idx)]
            valid_indices = sorted({i for i in idx_values if 1 <= i <= max_idx}, reverse=True)
            if not valid_indices:
                bot.send_message(
                    f"All indices out of range. You currently have {len(positions)} "
                    f"position{'s' if len(positions) != 1 else ''}. "
                    "Use /positions to see valid indices."
                )
                continue
            removed_msgs = []
            for idx in valid_indices:
                removed = positions.pop(idx - 1)
                removed_msgs.append(
                    f"{idx}. {removed.side} @ {removed.my_limit_price:.3f} on {removed.market_slug}"
                )
            save_positions(positions)
            msg_lines = ["Removed position(s):"] + removed_msgs
            if out_of_range:
                msg_lines.append(
                    "Ignored out-of-range index/indices: " + ", ".join(str(i) for i in sorted(set(out_of_range)))
                )
            if invalid_tokens:
                msg_lines.append(
                    "Ignored non-numeric token(s): " + ", ".join(sorted(set(invalid_tokens)))
                )
            bot.send_message("\n".join(msg_lines))

        elif cmd in {"/list_event", "/event"}:
            if len(parts) < 2:
                bot.send_message("Usage: /list_event <event-slug-or-url>")
            else:
                raw = parts[1]
                norm = normalize_market_slug(raw)
                sub_markets = fetch_event_markets(norm)
                if not sub_markets:
                    bot.send_message(f"No sub-markets found for event: <code>{norm}</code>")
                else:
                    lines = [f"<b>Sub-markets for</b> <code>{norm}</code>\n"]
                    for m in sub_markets:
                        slug = m.get("slug") or ""
                        question = (m.get("question") or slug)[:70]
                        outcomes = m.get("outcomePrices") or []
                        prices_str = ""
                        if outcomes:
                            try:
                                yes_p = float(outcomes[0])
                                no_p = float(outcomes[1]) if len(outcomes) > 1 else 1 - yes_p
                                prices_str = f"  YES {yes_p:.0%} / NO {no_p:.0%}"
                            except Exception:
                                pass
                        lines.append(f"• <b>{question}</b>{prices_str}\n  <code>{slug}</code>")
                    lines.append("\nUse: /add_position &lt;slug&gt; &lt;YES/NO&gt; &lt;price&gt;")
                    bot.send_message("\n".join(lines))

        elif cmd in {"/help", "/start"}:
            bot.send_message(
                "Commands:\n"
                "/positions — list current positions\n"
                "/out_of_range — list only OUT OF RANGE positions (distance ≥ 5¢)\n"
                "/market <slug-or-url> — show only positions for a specific market\n"
                "/list_event <slug-or-url> — list all sub-markets for an event (moneyline, spreads, totals)\n"
                "/add_position <slug> <YES/NO> <price> [notes]\n"
                "/edit_position <index> <new_price> — edit price of an existing position\n"
                "/bulk_add — add many positions; next message: one '<slug> <YES/NO> <price>' per line\n"
                "/remove_position <index> — remove by index from /positions\n\n"
                "While monitoring: Telegram alerts each poll when bids at/above your limit are "
                "below $1.2M (warning band) or below min_bid_depth_usd (critical; see monitor_config.json)."
            )

    return last_update_id


def run_position_monitor(
    positions: list[Position],
    bot: Optional[TelegramBot],
    poll_interval_seconds: int = 25,
    min_bid_depth_usd: float = 50000.0,
    wallet_address: str = "",
    sync_from_wallet: bool = False,
) -> None:
    """Continuously monitor positions and send alerts when price nears limit."""
    if not positions:
        print()
        print(
            "No positions loaded yet — Telegram commands (/list_event, /add_position) are active."
        )
        print("Add positions via Telegram or restart and enter positions in the terminal.")
        print()

    last_update_id: Optional[int] = None
    print()
    print("Starting position monitor. Ctrl+C to stop.")
    if sync_from_wallet and wallet_address:
        print(
            "Wallet sync ON: positions refresh every poll from your holdings (Data API). "
            "Selling/removing a position updates automatically. "
            "Unfilled LP-only limit orders are not listed. "
            "Telegram /add_position changes are overwritten on the next poll."
        )
    print(
        "Game countdown (from Polymarket schedule): "
        "green ≥7h — orange 4–7h — red <4h or GAME STARTED (exit ≥4h before tip)."
    )
    print(
        "Bid depth: total USD of bids at or above your limit (includes depth at your price); "
        f"red when below ${BIDS_BEFORE_DISPLAY_RED_USD:,.0f}; "
        "red + ⚠ when below your min alert threshold."
    )
    # Cache orderbooks per token_id within a single loop to avoid spamming API
    while True:
        if sync_from_wallet and wallet_address:
            fresh = positions_from_wallet_data_api(wallet_address)
            positions.clear()
            positions.extend(fresh)

        orderbook_cache: dict[str, Optional[dict]] = {}
        rows: list[dict] = []
        for idx, pos in enumerate(positions, 1):
            market = fetch_market_by_slug(pos.market_slug)
            if not market:
                print(f"  Could not fetch market for slug '{pos.market_slug}'.")
                continue
            yes_price, no_price = get_current_prices(market)
            current_price = yes_price if pos.side == "YES" else no_price
            distance_cents = abs(current_price - pos.my_limit_price) * 100

            # Total USD of bids at or above our limit (same book depth as UI "bids before";
            # includes all size at our price level, not only queue-ahead).
            yes_token_id, no_token_id = parse_token_ids(market)
            token_id = yes_token_id if pos.side == "YES" else no_token_id
            bids_dollars_before = 0.0
            if token_id:
                if token_id not in orderbook_cache:
                    orderbook_cache[token_id] = fetch_orderbook(token_id)
                ob = orderbook_cache.get(token_id) or {}
                bids = ob.get("bids", []) or []
                for b in bids:
                    try:
                        price = float(b.get("price", 0))
                        size = float(
                            b.get("quantity")
                            or b.get("size")
                            or b.get("remaining")
                            or 0
                        )
                    except Exception:
                        continue
                    if price >= pos.my_limit_price:
                        bids_dollars_before += price * size

            # Collect row for sorted display
            event_slug = market.get("eventSlug") or ""
            market_slug = market.get("slug") or normalize_market_slug(pos.market_slug)
            url = f"https://polymarket.com/event/{event_slug}/{market_slug}" if event_slug else f"https://polymarket.com/event/{market_slug}"
            rows.append(
                {
                    "idx": idx,
                    "url": url,
                    "question": market.get("question") or url,
                    "side": pos.side,
                    "current_price": current_price,
                    "limit_price": pos.my_limit_price,
                    "distance_cents": distance_cents,
                    "bids_before": bids_dollars_before,
                    "game_start": parse_market_game_start_utc(market),
                }
            )

            # Telegram: bid depth — notify every poll while condition holds (no dedup).
            # Below min: critical only. Between min and $1.2M: $1.2M warning (not both).
            if bids_dollars_before < min_bid_depth_usd:
                question = (market.get("question") or pos.market_slug)[:80]
                msg = (
                    "⚠️ <b>LOW BID DEPTH ALERT</b>\n\n"
                    f"<b>{idx}. {question}</b>\n\n"
                    f"Total USD of bids <b>at or above</b> your <b>{pos.side}</b> limit "
                    f"({pos.my_limit_price:.3f}) is below <b>${min_bid_depth_usd:,.0f}</b>.\n"
                    f"• Bids at/above limit: <b>${bids_dollars_before:,.2f}</b>\n"
                    f"• Threshold: <b>${min_bid_depth_usd:,.0f}</b>\n\n"
                    f"(Includes all liquidity at your price level.)\n\n"
                    f"Consider cancelling this position.\n"
                    f"<a href='{url}'>View market</a>"
                )
                print(
                    f"  >> Low bid depth (at/above limit)! "
                    f"(${bids_dollars_before:,.2f} < ${min_bid_depth_usd:,.0f}) Alerting."
                )
                if bot is not None:
                    bot.send_message(msg)
            elif bids_dollars_before < BIDS_BEFORE_DISPLAY_RED_USD:
                question = (market.get("question") or pos.market_slug)[:80]
                msg = (
                    "📉 <b>BID DEPTH BELOW $1.2M</b>\n\n"
                    f"<b>{idx}. {question}</b>\n\n"
                    f"Total USD of bids <b>at or above</b> your <b>{pos.side}</b> limit "
                    f"({pos.my_limit_price:.3f}) is <b>below ${BIDS_BEFORE_DISPLAY_RED_USD:,.0f}</b>.\n"
                    f"• Bids at/above limit: <b>${bids_dollars_before:,.2f}</b>\n\n"
                    f"<a href='{url}'>View market</a>"
                )
                print(
                    f"  >> Bid depth below $1.2M! (${bids_dollars_before:,.2f}) Alerting."
                )
                if bot is not None:
                    bot.send_message(msg)

        # After processing all positions, print sorted by soonest game first (then distance)
        if rows:
            now_utc = datetime.now(timezone.utc)
            rows.sort(key=lambda r: position_row_sort_key(r, now_utc))
            print()
            for r in rows:
                idx = r["idx"]
                dist = r["distance_cents"]
                # Color coding:
                # - <=1¢: red (very close)
                # - <=2¢: amber
                # - >=5¢: bright red + "OUT OF RANGE"
                # - else (2–4.9¢): green
                if dist <= 1.0:
                    dist_str = color_text(f"{dist:.1f}¢", RED)
                elif dist <= 2.0:
                    dist_str = color_text(f"{dist:.1f}¢", YELLOW)
                elif dist >= 5.0:
                    dist_str = color_text(f"{dist:.1f}¢ OUT OF RANGE", RED)
                else:
                    dist_str = color_text(f"{dist:.1f}¢", GREEN)
                title = r.get("question") or r["url"]
                if len(title) > 120:
                    title = title[:117] + "..."
                if USE_COLOR:
                    title = color_text(title, BOLD)
                bb = float(r.get("bids_before", 0.0))
                bb_str = format_bids_before_terminal(bb, min_bid_depth_usd)
                game_str = format_game_countdown_colored(r.get("game_start"), now_utc)
                print(
                    f"{idx}. {title} — {r['side']} "
                    f"current: {r['current_price']:.3f}, "
                    f"limit: {r['limit_price']:.3f}, "
                    f"distance: {dist_str}, "
                    f"bids before (at/above limit): {bb_str}, "
                    f"{game_str}"
                )
        # Handle Telegram commands (positions management)
        last_update_id = process_telegram_commands(bot, positions, last_update_id)

        print()
        print(f"Sleeping {poll_interval_seconds} seconds before next check...")
        try:
            time.sleep(poll_interval_seconds)
        except KeyboardInterrupt:
            print("\nStopping monitor.")
            break


def main():
    if len(sys.argv) > 1 and sys.argv[1] in ("--telegram-test", "-t"):
        sys.exit(send_telegram_test())

    print("Polymarket LP Rewards — Best low-risk markets")
    print()
    print("Select mode:")
    print("  [1] Scan low-risk LP markets")
    print("  [2] Monitor my LP positions (bid depth + terminal)")
    print("  [3] Scan markets, then monitor positions")
    print("  [4] Export all active NCAA CBB market slugs (moneyline/spreads/totals → files)")
    mode = input("Choose mode [1/2/3/4] (default 1): ").strip() or "1"

    run_scan = mode in {"1", "3"}
    run_monitor = mode in {"2", "3"}
    export_cbb = mode == "4"

    if export_cbb:
        export_ncaa_cbb_market_slugs()
        return

    if run_scan:
        print()
        print("Fetching active markets (paginated)...")
        all_markets = fetch_all_markets()
        print(f"Total markets: {len(all_markets)}")
        reward_markets = filter_reward_markets(all_markets)
        print(f"Markets with LP rewards (rewardsDailyRate > 0): {len(reward_markets)}")
        if not reward_markets:
            print("No reward markets found.")
        else:
            rows = []
            for m in reward_markets:
                row = build_market_row(m)
                if row is not None:
                    rows.append(row)
            # Exclude asset-price markets (commodity, crypto, stock) — one pump can change things a lot
            # Also require minimum total volume of $25,000 USD
            low_risk = [
                r
                for r in rows
                if r["risk_composite"] <= MAX_RISK_FOR_DISPLAY
                and r.get("event_category") != "asset_price"
                and r.get("volume", 0) >= 25000
            ]
            low_risk.sort(key=lambda r: (-r["capital_efficiency"], r["risk_composite"]))
            top = low_risk[:TOP_N]
            print()
            print(
                f"Markets with minimal risk (composite risk ≤ {MAX_RISK_FOR_DISPLAY}): {len(low_risk)}"
            )
            print(f"Showing top {len(top)} by capital efficiency (then by lowest risk):")
            print()
            sep = "-" * 100
            print(sep)
            for i, r in enumerate(top, 1):
                risk_score = r["risk_composite"]
                risk_label_col = colored_risk_label(risk_score)
                title = color_text(r["question"], BOLD) if USE_COLOR else r["question"]
                print(f"  {i}. {title}")
                print(
                    f"     Risk: {risk_score} ({risk_label_col})  "
                    f"Spike: {r['risk_spike']}  Time: {r['risk_time']}  Adverse: {r['risk_adverse']}  "
                    f"Category: {r['event_category']}"
                )
                print(
                    f"     Daily rewards: ${r['daily_rewards']:.2f}  "
                    f"Days left: {r['days_remaining']}  "
                    f"Est. min capital: ${r['min_capital_estimate']:,.0f}  "
                    f"Est. APY: {r['estimated_apy']:.1f}%  "
                    f"Total vol: ${r.get('volume', 0):,.0f}  Liquidity: ${r.get('liquidity', 0):,.0f}"
                )
                url_str = color_text(r["url"], CYAN) if USE_COLOR else r["url"]
                print(f"     {url_str}")
                print(f"     Reasoning — {format_reasoning(r)}")
                print(sep)
                print()
            if not top:
                print(
                    "No markets in the minimal-risk range. Try raising MAX_RISK_FOR_DISPLAY in the script."
                )
            print()
            print("Scan complete.")

    if run_monitor:
        print()
        bot, poll_interval, min_bid_depth, wallet_address, sync_wallet = (
            get_monitor_config_with_persistence()
        )
        if sync_wallet and wallet_address:
            print()
            print("Loading positions from Polymarket Data API (wallet holdings)...")
            positions = positions_from_wallet_data_api(wallet_address)
            print(f"  {len(positions)} position(s) with size > 0.")
            if not positions:
                print(
                    "  Tip: unfilled LP limit orders do not appear here — "
                    "disable wallet sync in monitor_config.json and use positions.json / Telegram."
                )
        else:
            positions = get_positions_with_persistence()
            if not positions:
                print(
                    "No positions in JSON yet — starting monitor anyway so Telegram works "
                    "(/list_event, /add_position). Alerts run once you add at least one position."
                )
        run_position_monitor(
            positions,
            bot,
            poll_interval_seconds=poll_interval,
            min_bid_depth_usd=min_bid_depth,
            wallet_address=wallet_address,
            sync_from_wallet=bool(sync_wallet and wallet_address),
        )


if __name__ == "__main__":
    main()
