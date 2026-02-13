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
DATA_API_BASE = "https://data-api.polymarket.com/positions"
PAGE_LIMIT = 100
REQUEST_TIMEOUT = 30
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
USE_COLOR = sys.stdout.isatty()


def color_text(text: str, color: str) -> str:
    """Wrap text in an ANSI color if supported."""
    if not USE_COLOR:
        return text
    return f"{color}{text}{RESET}"


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


def fetch_all_markets():
    """Fetch all active, non-closed markets with pagination."""
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
        print(f"  Fetched {len(all_markets)} markets...", flush=True)
    return all_markets


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


def show_user_positions_read_only() -> None:
    """
    Prompt for a Polymarket user/proxy wallet address and display current positions
    from the public Data API. This is read-only and does not require a private key.
    """
    print()
    print("Read-only Polymarket positions (via Data API)")
    addr = input("Enter your Polymarket user/proxy wallet address (0x...): ").strip()
    if not addr:
        print("No address entered; skipping.")
        return

    print()
    print(f"Fetching current positions for {addr} ...")
    positions = fetch_user_positions(addr)
    if not positions:
        print("No open positions returned by the Data API for this address.")
        return

    print(f"Found {len(positions)} position(s).")
    print()
    sep = "-" * 100
    for idx, p in enumerate(positions, 1):
        title = p.get("title") or "(untitled market)"
        outcome = p.get("outcome") or "N/A"
        size = float(p.get("size", 0) or 0)
        avg_price = float(p.get("avgPrice", 0) or 0)
        cur_price = float(p.get("curPrice", 0) or 0)
        cash_pnl = float(p.get("cashPnl", 0) or 0)
        pct_pnl = float(p.get("percentPnl", 0) or 0)
        slug = p.get("slug") or ""
        event_slug = p.get("eventSlug") or ""
        if event_slug and slug:
            url = f"https://polymarket.com/event/{event_slug}/{slug}"
        elif slug:
            url = f"https://polymarket.com/event/{slug}"
        else:
            url = ""

        if len(title) > 120:
            title = title[:117] + "..."
        if USE_COLOR:
            title_out = color_text(title, BOLD)
        else:
            title_out = title

        print(sep)
        print(f"{idx}. {title_out}")
        print(
            f"   Outcome: {outcome}  "
            f"Size: {size:.4f}  "
            f"Avg price: {avg_price:.4f}  "
            f"Current price: {cur_price:.4f}"
        )
        print(
            f"   PnL: ${cash_pnl:,.2f}  "
            f"Percent PnL: {pct_pnl:.2f}%"
        )
        if url:
            url_str = color_text(url, CYAN) if USE_COLOR else url
            print(f"   {url_str}")
        print()
    print(sep)


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


def is_crypto_up_down_market(question: str) -> bool:
    """Check if market is a crypto or stock index 'Up or Down' price prediction market."""
    q = (question or "").lower()
    asset_keywords = [
        "bitcoin", "btc", "ethereum", "eth", "solana", "sol", "xrp", "crypto",
        "spx", "s&p", "sp500", "s&p 500", "nasdaq", "dow", "stock"
    ]
    up_down_patterns = ["up or down", "up/down", "up or down market"]
    return any(asset in q for asset in asset_keywords) and any(pattern in q for pattern in up_down_patterns)


def parse_time_period_from_question(question: str) -> Optional[tuple[datetime, datetime]]:
    """
    Parse time period from question text like:
    'Bitcoin Up or Down - February 13, 12:00PM-12:05PM ET'
    Returns (start_time, end_time) in UTC, or None if can't parse.
    """
    if not question:
        return None
    
    # Look for patterns like "February 13, 12:00PM-12:05PM ET" or "Feb 13, 12:00PM-4:00PM ET"
    # Try to extract date and time range
    import re
    
    # Pattern: Month Day, HH:MMAM/PM-HH:MMAM/PM ET
    pattern = r"([A-Za-z]+)\s+(\d{1,2}),\s+(\d{1,2}):(\d{2})(AM|PM)\s*-\s*(\d{1,2}):(\d{2})(AM|PM)\s*(ET|EST|EDT)"
    match = re.search(pattern, question, re.IGNORECASE)
    if not match:
        return None
    
    try:
        month_name, day, start_hour, start_min, start_ampm, end_hour, end_min, end_ampm, tz = match.groups()
        
        # Get current year (assume same year unless it's past December and we're in January)
        now = datetime.now(timezone.utc)
        year = now.year
        
        # Convert month name to number
        month_map = {
            "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
            "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12
        }
        month = month_map.get(month_name.lower())
        if not month:
            return None
        
        # Convert to 24-hour format
        start_hour_int = int(start_hour)
        if start_ampm.upper() == "PM" and start_hour_int != 12:
            start_hour_int += 12
        elif start_ampm.upper() == "AM" and start_hour_int == 12:
            start_hour_int = 0
        
        end_hour_int = int(end_hour)
        if end_ampm.upper() == "PM" and end_hour_int != 12:
            end_hour_int += 12
        elif end_ampm.upper() == "AM" and end_hour_int == 12:
            end_hour_int = 0
        
        # ET is UTC-5 (EST) or UTC-4 (EDT) - use UTC-4 for simplicity (EDT)
        # Create naive datetime objects (assume ET timezone)
        start_naive = datetime(year, month, int(day), start_hour_int, int(start_min))
        end_naive = datetime(year, month, int(day), end_hour_int, int(end_min))
        
        # Convert ET to UTC (ET = UTC-4, so add 4 hours)
        start_dt = (start_naive + timedelta(hours=4)).replace(tzinfo=timezone.utc)
        end_dt = (end_naive + timedelta(hours=4)).replace(tzinfo=timezone.utc)
        
        return start_dt, end_dt
    except Exception:
        return None


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
            positions = prompt_for_positions()
    else:
        positions = prompt_for_positions()

    if positions:
        save_positions(positions)
    return positions


def prompt_for_telegram_bot() -> Optional[TelegramBot]:
    """Ask user for Telegram bot token and chat id."""
    print()
    print("Telegram alerts setup (for price approaching your bids).")
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


def get_monitor_config_with_persistence() -> tuple[Optional[TelegramBot], int, float]:
    """
    Load saved Telegram/settings config if available, optionally override via prompts,
    and persist latest settings.
    Returns (TelegramBot|None, poll_interval_seconds, price_alert_threshold_cents).
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
            poll_interval = int(settings.get("poll_interval_seconds", 30))
            price_thresh = float(settings.get("price_alert_threshold_cents", 1.0))
            return bot, poll_interval, price_thresh
        else:
            print("Discarding saved monitor config for this run; enter new settings.")

    # Fresh prompts
    bot = prompt_for_telegram_bot()
    try:
        poll_str = input("Poll interval seconds [default 30]: ").strip()
        poll_interval = int(poll_str) if poll_str else 30
    except ValueError:
        poll_interval = 30
    try:
        thresh_str = input("Price alert threshold in cents [default 1]: ").strip()
        price_thresh = float(thresh_str) if thresh_str else 1.0
    except ValueError:
        price_thresh = 1.0

    # Save for next time
    cfg = {
        "telegram": {},
        "settings": {
            "poll_interval_seconds": poll_interval,
            "price_alert_threshold_cents": price_thresh,
        },
    }
    if bot is not None:
        cfg["telegram"] = {"bot_token": bot.token, "chat_id": bot.chat_id}
    save_monitor_config(cfg)
    return bot, poll_interval, price_thresh


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
                        }
                    )

                # Sort like terminal: smallest distance, then fewest bids_before
                rows.sort(
                    key=lambda r: (
                        r["distance_cents"] if r["distance_cents"] is not None else 1e9,
                        r["bids_before"] if r["bids_before"] is not None else 1e9,
                    )
                )

                # Build message chunks under Telegram limit
                header = "<b>Current positions</b>\n(sorted by risk — closest & thinnest first):"
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
                            f"Bids before: <b>${bids:,.2f}</b>"
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
                        }
                    )

                if not rows:
                    bot.send_message("No OUT OF RANGE positions (distance ≥ 5¢).")
                else:
                    rows.sort(
                        key=lambda r: (
                            r["distance_cents"],
                            r["bids_before"],
                        )
                    )
                    header = "<b>OUT OF RANGE positions</b>\n(distance ≥ 5¢; closest & thinnest first):"
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
                            f"Bids before: <b>${bids:,.2f}</b>"
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
                        }
                    )

                if not rows:
                    bot.send_message(
                        "No positions found for that market. "
                        "Make sure you used the slug or URL of a market you have saved."
                    )
                else:
                    rows.sort(
                        key=lambda r: (
                            r["distance_cents"],
                            r["bids_before"],
                        )
                    )
                    # Use the first row's question as market title
                    title = rows[0]["question"]
                    if len(title) > 120:
                        title = title[:117] + "..."
                    header = (
                        "<b>Positions for market</b>\n"
                        f"{title}\n"
                        "(sorted by risk — closest & thinnest first):"
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
                            f"Bids before: <b>${bids:,.2f}</b>"
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

        elif cmd in {"/help", "/start"}:
            bot.send_message(
                "Commands:\n"
                "/positions — list current positions\n"
                "/out_of_range — list only OUT OF RANGE positions (distance ≥ 5¢)\n"
                "/market <slug-or-url> — show only positions for a specific market\n"
                "/add_position <slug> <YES/NO> <price> [notes]\n"
                "/edit_position <index> <new_price> — edit price of an existing position\n"
                "/bulk_add — add many positions; next message: one '<slug> <YES/NO> <price>' per line\n"
                "/remove_position <index> — remove by index from /positions\n"
            )

    return last_update_id


def check_crypto_up_down_markets(
    bot: Optional[TelegramBot],
    alerted_markets: set[str],
) -> None:
    """Check for new Up/Down markets (crypto or stock indices) starting within 1.5 hours and send Telegram alerts."""
    if bot is None:
        return
    
    try:
        reward_markets = filter_reward_markets(fetch_all_markets())
        now = datetime.now(timezone.utc)
        
        new_markets = []
        for m in reward_markets:
            question = m.get("question", "")
            if not is_crypto_up_down_market(question):
                continue
            
            slug = m.get("slug", "")
            if slug in alerted_markets:
                continue
            
            time_period = parse_time_period_from_question(question)
            if not time_period:
                continue
            
            start_time, end_time = time_period
            hours_until_start = (start_time - now).total_seconds() / 3600
            
            # Only alert if market starts within 1.5 hours (and hasn't started yet)
            if hours_until_start > 1.5 or hours_until_start < 0:
                continue
            
            row = build_market_row(m)
            if row:
                new_markets.append({
                    "slug": slug,
                    "question": question,
                    "start_time": start_time,
                    "hours_until_start": hours_until_start,
                    "daily_rewards": row["daily_rewards"],
                    "url": row["url"],
                })
        
        # Alert on new markets
        for market in new_markets:
            alerted_markets.add(market["slug"])
            start_str = market["start_time"].strftime("%Y-%m-%d %H:%M UTC")
            msg = (
                "🚀 <b>UP/DOWN MARKET OPPORTUNITY</b>\n\n"
                f"<b>{market['question']}</b>\n\n"
                f"• Start: <b>{start_str}</b> ({market['hours_until_start']:.1f} hours from now)\n"
                f"• Daily rewards: <b>${market['daily_rewards']:.2f}</b>\n"
                f"• <b>Zero risk until market opens</b> (price cannot move when closed)\n\n"
                f"<a href='{market['url']}'>View market</a>"
            )
            bot.send_message(msg)
            print(f"  >> Alerted on Up/Down market: {market['question'][:60]}...")
    except Exception as e:
        print(f"  Error checking crypto Up/Down markets: {e}", file=sys.stderr)


def run_position_monitor(
    positions: list[Position],
    bot: Optional[TelegramBot],
    poll_interval_seconds: int = 30,
    price_alert_threshold_cents: float = 1.0,
) -> None:
    """Continuously monitor positions and send alerts when price nears limit."""
    if not positions:
        print("No positions to monitor.")
        return

    last_alert_price: dict[tuple[str, str], float] = {}
    last_update_id: Optional[int] = None
    alerted_crypto_markets: set[str] = set()
    iteration_count = 0
    print()
    print("Starting position monitor. Ctrl+C to stop.")
    # Cache orderbooks per token_id within a single loop to avoid spamming API
    while True:
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
            key = (pos.market_slug, pos.side)

            # Compute total dollars of bids at or above our limit on this side
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
                    # We care about bids at our price or better (>= limit)
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
                }
            )

            if distance_cents <= price_alert_threshold_cents:
                # Avoid duplicate alerts at same price
                if last_alert_price.get(key) == current_price:
                    continue
                last_alert_price[key] = current_price

                direction = (
                    "rising toward" if current_price < pos.my_limit_price else "falling toward"
                )
                question = (market.get("question") or pos.market_slug)[:80]
                msg = (
                    "🚨 <b>PRICE ALERT</b>\n\n"
                    f"<b>{idx}. {question}</b>\n\n"
                    f"Price {direction} your limit on <b>{pos.side}</b>.\n"
                    f"• Current: <b>{current_price:.3f}</b>\n"
                    f"• Your limit: <b>{pos.my_limit_price:.3f}</b>\n"
                    f"• Distance: <b>{distance_cents:.1f}¢</b>\n\n"
                    f"<a href='https://polymarket.com/event/{pos.market_slug}'>View market</a>"
                )
                print("  >> Price near your limit! Alerting.")
                if bot is not None:
                    bot.send_message(msg)
        # After processing all positions, print sorted by distance (closest first)
        if rows:
            # Sort by riskiness: smallest distance AND fewest bids before at top
            rows.sort(
                key=lambda r: (
                    r["distance_cents"],
                    r.get("bids_before", 0.0),
                )
            )
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
                # Slightly shorten very long questions
                if len(title) > 120:
                    title = title[:117] + "..."
                if USE_COLOR:
                    title = color_text(title, BOLD)
                print(
                    f"{idx}. {title} — {r['side']} "
                    f"current: {r['current_price']:.3f}, "
                    f"limit: {r['limit_price']:.3f}, "
                    f"distance: {dist_str}, "
                    f"bids before: ${r.get('bids_before', 0.0):,.2f}"
                )
        # Handle Telegram commands (positions management)
        last_update_id = process_telegram_commands(bot, positions, last_update_id)
        
        # Check for crypto Up/Down markets every 10 iterations (~5 minutes)
        iteration_count += 1
        if iteration_count % 10 == 0:
            check_crypto_up_down_markets(bot, alerted_crypto_markets)
        
        print()
        print(f"Sleeping {poll_interval_seconds} seconds before next check...")
        try:
            time.sleep(poll_interval_seconds)
        except KeyboardInterrupt:
            print("\nStopping monitor.")
            break


def main():
    print("Polymarket LP Rewards — Best low-risk markets")
    print()
    print("Select mode:")
    print("  [1] Scan low-risk LP markets")
    print("  [2] Monitor my LP positions (price alerts)")
    print("  [3] Scan markets, then monitor positions")
    print("  [4] Show my Polymarket positions by address (read-only, no private key)")
    mode = input("Choose mode [1/2/3/4] (default 1): ").strip() or "1"

    run_scan = mode in {"1", "3"}
    run_monitor = mode in {"2", "3"}
    show_positions = mode == "4"

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
        positions = get_positions_with_persistence()
        if not positions:
            print("No positions entered; skipping monitor.")
            return
        bot, poll_interval, price_thresh = get_monitor_config_with_persistence()
        run_position_monitor(
            positions,
            bot,
            poll_interval_seconds=poll_interval,
            price_alert_threshold_cents=price_thresh,
        )
    elif show_positions:
        show_user_positions_read_only()


if __name__ == "__main__":
    main()
