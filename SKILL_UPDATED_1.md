---
name: polymarket-lp-rewards
description: Fetch and analyze Polymarket liquidity provider reward markets. Calculates risk scores based on volatility and time to resolution, plus capital efficiency metrics. Outputs structured JSON/CSV data for further analysis.
---

# Polymarket LP Rewards Analyzer

This skill helps identify profitable liquidity provision opportunities on Polymarket by fetching all markets with LP rewards and analyzing them for risk and capital efficiency.

## When to Use This Skill

Use this skill when the user wants to:
- Find all Polymarket markets currently offering LP rewards
- Analyze LP reward opportunities by risk and profitability
- Get structured data (JSON/CSV) about reward markets for external analysis
- Identify the best markets to provide liquidity on

Trigger phrases include:
- "fetch polymarket LP rewards"
- "analyze polymarket liquidity rewards"
- "which polymarket markets have rewards"
- "find best polymarket LP opportunities"

## Core Workflow

1. **Fetch Reward Markets**: Get all active markets with `clobRewards` from Polymarket API
2. **Filter for Active Rewards**: Only include markets where `rewardsDailyRate > 0`
3. **Calculate Risk Score**: Assess volatility and time-to-resolution risk
4. **Calculate Capital Efficiency**: Determine rewards per dollar of required liquidity
5. **Output Structured Data**: Return JSON or CSV with all metrics

## API Integration

### Polymarket Gamma API

The primary data source is the Gamma API at `https://gamma-api.polymarket.com`

> ⚠️ **IMPORTANT**: As of Feb 2026, there are 27,000+ active markets. Pagination is essential.

**Fetching reward markets:**
```python
import requests
from datetime import datetime

# Fetch all active markets with pagination
def fetch_all_markets():
    all_markets = []
    offset = 0
    limit = 100
    
    while True:
        response = requests.get(
            "https://gamma-api.polymarket.com/markets",
            params={
                "active": "true",
                "closed": "false",
                "limit": limit,
                "offset": offset
            },
            timeout=30
        )
        markets = response.json()
        
        if not markets:
            break
            
        all_markets.extend(markets)
        
        if len(markets) < limit:
            break
            
        offset += limit
    
    return all_markets

markets = fetch_all_markets()

# Filter for markets with rewards AND non-zero daily rate
reward_markets = [
    market for market in markets 
    if market.get("clobRewards") 
    and len(market["clobRewards"]) > 0
    and float(market["clobRewards"][0].get("rewardsDailyRate", 0)) > 0
]
```

### Actual clobRewards Structure

> ⚠️ **CRITICAL CORRECTION**: The actual API response differs from older documentation.

**Real clobRewards structure (as of Feb 2026):**
```json
{
  "clobRewards": [
    {
      "id": "12693",
      "conditionId": "0x49686d26fb712515...",
      "assetAddress": "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",
      "rewardsAmount": 0,
      "rewardsDailyRate": 100,
      "startDate": "2025-01-04",
      "endDate": "2500-12-31"
    }
  ]
}
```

**Key fields to extract:**

| Field | Location | Description |
|-------|----------|-------------|
| `question` | market | Market question/title |
| `slug` | market | URL slug for market link |
| `clobTokenIds` | market | Token IDs for YES/NO outcomes |
| `rewardsDailyRate` | clobRewards[0] | **Daily USD rewards** (USE THIS, not rewardsAmount) |
| `rewardsAmount` | clobRewards[0] | Often 0, less reliable than dailyRate |
| `spread` | market | Current spread as decimal (0.01 = 1 cent) |
| `competitive` | market | Competition metric (0-1, higher = more competitive) |
| `volume` | market | Total trading volume in USD |
| `liquidity` | market | Current liquidity in pool |
| `endDate` | market | Market resolution date (ISO format) |
| `outcomePrices` | market | JSON string of YES/NO prices, e.g., `["0.85", "0.15"]` |
| `bestBid` / `bestAsk` | market | Current best bid/ask prices |

> ⚠️ **Fields that DON'T exist**: `rewardsMinSize`, `rewardsMaxSpread` — these must be estimated from market-level data.

### CLOB API for Orderbook Data

For additional volatility metrics, fetch orderbook data:
```python
# Get orderbook for a token
response = requests.get(
    "https://clob.polymarket.com/book",
    params={"token_id": token_id}
)
orderbook = response.json()
```

## Risk Scoring Algorithm

> ⚠️ **Core Insight**: The real LP risk is **adverse selection** — getting picked off by informed traders when news breaks. You're providing liquidity at stale prices while someone with fresh information trades against you.

Calculate a composite risk score (0-100, lower is better) based on three factors:

### 1. Spike Risk (50% weight) — Can price move 30%+ in minutes?

This is the most important factor. Ask: **"What would cause this market to suddenly resolve or move dramatically?"**

**Event categories and their spike profiles:**

| Category | Spike Risk | Why |
|----------|-----------|-----|
| Political/Geopolitical | EXTREME | Single headline can resolve it ("PM resigns", "missiles launched") |
| Binary yes/no events | VERY HIGH | Resolution is instant 0→100 or 100→0 |
| **Election / primary / nomination** | **HIGH** | **Scheduled binary**: you know WHEN the spike is (e.g. primary day), but not which way it goes. Worst of both. |
| Scheduled announcements | HIGH | Known spike window (Fed meeting, earnings, game day) |
| **Asset price markets** | **EXCLUDE** | **Do not recommend for low-risk LP.** Bets on commodity, crypto, or stock price (e.g. "Silver hit $130", "BTC above $150k"): one pump or dump can move price violently. |
| Long-duration milestones | LOW | Gradual probability shifts over months (non–price events) |
| Continuous metrics | LOW | Price adjusts incrementally with data (non–asset-price) |

```python
import re

def classify_event_type(question: str) -> dict:
    """
    Analyze the market question to determine event characteristics.
    Returns spike risk assessment.
    """
    q = question.lower()
    
    # Keywords indicating binary/sudden resolution
    binary_triggers = [
        'resign', 'resigns', 'out as', 'step down', 'fired', 'removed',
        'strike', 'strikes', 'attack', 'invade', 'invasion', 'war',
        'die', 'dies', 'death', 'assassin',
        'announce', 'announcement', 'declare',
        'shut down', 'shutdown', 'default',
        'ceasefire', 'peace deal', 'treaty'
    ]
    
    # Keywords indicating scheduled events (known spike window)
    scheduled_triggers = [
        'fed ', 'fomc', 'interest rate', 'rate cut', 'rate hike',
        'election', 'vote', 'referendum',
        'nominee', 'nomination', 'primary', 'democratic nominee',
        'republican nominee', 'general election',
        'super bowl', 'world cup', 'championship', 'finals',
        'earnings', 'quarterly', 'q1', 'q2', 'q3', 'q4',
        'meeting', 'summit', 'conference'
    ]
    
    # Congressional district pattern (PA-03, FL-19, NY-14) = scheduled primary/nomination
    district_pattern = re.compile(r'\b[A-Z]{2}-\d{1,2}\b')
    
    # Asset price markets: EXCLUDE from low-risk LP. One pump/dump can move price violently.
    # Do not treat as "gradual" — betting on commodity, crypto, or stock price is high risk for LPs.
    asset_price_triggers = [
        'bitcoin', 'btc', 'eth', 'crypto', 'price above', 'price below',
        'stock', 's&p', 'nasdaq', 'dow', 'spx', 'sp500',
        'silver', 'gold', ' hit ', ' above $', ' below $', 'close over', 'close above', 'close below',
        'gc)', 'si)', ' (si)', ' (gc)'  # commodity tickers in titles
    ]
    
    # Keywords indicating gradual/continuous events (non–asset-price only)
    gradual_triggers = [
        'gdp', 'inflation', 'unemployment',
        'subscribers', 'followers', 'views', 'streams',
        'before gta', 'by end of year', 'by 2027', 'by 2028'
    ]
    
    # Check for binary/sudden events
    is_binary = any(trigger in q for trigger in binary_triggers)
    
    # Check for scheduled events
    is_scheduled = any(trigger in q for trigger in scheduled_triggers)
    # Congressional district (e.g. PA-03, FL-19) implies scheduled primary/nomination
    if district_pattern.search(question):
        is_scheduled = True
    
    # Check for asset price markets first — EXCLUDE from low-risk recommendations
    is_asset_price = any(trigger in q for trigger in asset_price_triggers)
    
    # Check for gradual events (no asset-price triggers)
    is_gradual = any(trigger in q for trigger in gradual_triggers)
    
    # Determine spike risk score (0-100)
    if is_asset_price:
        base_spike_risk = 72  # One pump/dump can change things a lot — do not recommend for low-risk LP
    elif is_binary:
        base_spike_risk = 85  # Can resolve instantly on single headline
    elif is_scheduled:
        base_spike_risk = 65  # Known spike window, but predictable timing
    elif is_gradual:
        base_spike_risk = 25  # Non–price milestones, incremental data
    else:
        base_spike_risk = 50  # Unknown, assume moderate
    
    category = 'asset_price' if is_asset_price else ('binary' if is_binary else 'scheduled' if is_scheduled else 'gradual' if is_gradual else 'unknown')
    return {
        'spike_risk': base_spike_risk,
        'is_binary': is_binary,
        'is_scheduled': is_scheduled,
        'is_gradual': is_gradual,
        'is_asset_price': is_asset_price,
        'category': category
    }
```

### 2. Time Proximity Risk (30% weight) — Exponential, not linear!

> ⚠️ **Critical**: 3 hours out is EXPONENTIALLY more dangerous than 3 days out.

The closer to resolution, the higher the chance that the NEXT piece of news is THE deciding news. This relationship is exponential.

**Known spike date (e.g. elections):** For many markets the *market end date* is not the real risk moment. Example: "Will X be the Democratic nominee for PA-03?" — the market may close June 2026, but the **actual resolution event** is primary day (e.g. May 19). As you approach May 19, risk should spike exponentially regardless of "days remaining" to the listed end date. When you have a known spike date (primary date, election day, Fed meeting), pass it as `known_spike_date_str` so time proximity is driven by the **nearer** of end date and spike date.

```python
from datetime import datetime, timezone

def calculate_time_proximity_risk(end_date_str: str, known_spike_date_str: str | None = None) -> int:
    """
    Returns risk score 0-100 based on time until resolution.
    Uses exponential scaling — hours away is catastrophically risky.
    If known_spike_date_str is set (e.g. primary day, election day), risk is based on
    the nearer of end_date and known_spike_date so risk spikes as you approach the event.
    """
    now = datetime.now(timezone.utc)
    candidates = []
    for label, s in [("end", end_date_str), ("spike", known_spike_date_str)]:
        if not s:
            continue
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
            candidates.append((d - now).total_seconds() / 3600)
        except Exception:
            continue
    if not candidates:
        return 40  # Unknown = moderate risk
    hours_remaining = min(candidates)  # Use nearer of end date vs known spike date
    
    if hours_remaining < 0:
        return 100  # Already expired — extreme risk
    elif hours_remaining < 6:
        return 98   # < 6 hours: EXTREME — any news is THE news
    elif hours_remaining < 24:
        return 90   # < 1 day: VERY HIGH — single news cycle decides it
    elif hours_remaining < 72:
        return 75   # < 3 days: HIGH — limited time to react
    elif hours_remaining < 168:
        return 55   # < 1 week: MODERATE-HIGH
    elif hours_remaining < 720:
        return 35   # < 1 month: MODERATE — time to adjust
    elif hours_remaining < 2160:
        return 20   # < 3 months: LOW
    else:
        return 8    # 3+ months: MINIMAL — long runway
```

### 3. Adverse Selection Risk (20% weight) — How badly can you get picked off?

When informed traders hit your quotes, how much do you lose?

```python
import json

def calculate_adverse_selection_risk(market: dict) -> float:
    """
    Assesses how vulnerable you are to informed traders.
    """
    # Parse price
    outcome_prices = market.get('outcomePrices', '["0.5", "0.5"]')
    try:
        if isinstance(outcome_prices, str):
            prices = json.loads(outcome_prices.replace("'", '"'))
        else:
            prices = outcome_prices
        yes_price = float(prices[0]) if prices else 0.5
    except:
        yes_price = 0.5
    
    # 1. Price extremity risk (0-40 points)
    # At 5¢ or 95¢, a resolution spike means 95¢ loss in one direction
    # At 50¢, max loss is 50¢ either way
    # Extreme prices = asymmetric massive loss potential
    price_distance = abs(yes_price - 0.50)
    extremity_risk = price_distance * 80  # 0-40 points
    
    # 2. Thin liquidity risk (0-30 points)
    # Less liquidity = easier to get run over
    liquidity = float(market.get('liquidity', 0) or 0)
    if liquidity < 10000:
        liquidity_risk = 30
    elif liquidity < 50000:
        liquidity_risk = 20
    elif liquidity < 200000:
        liquidity_risk = 10
    else:
        liquidity_risk = 5
    
    # 3. Competition/repricing speed (0-30 points)
    # More LPs = faster collective repricing = less adverse selection
    competitive = float(market.get('competitive', 0) or 0)
    competition_risk = (1 - competitive) * 30  # Lower competition = higher risk
    
    total = extremity_risk + liquidity_risk + competition_risk
    return min(total, 100)
```

### Composite Risk Score

```python
def calculate_risk_score(market: dict) -> dict:
    """
    Comprehensive risk assessment combining:
    - Spike risk (50%): Can price move 30%+ in minutes?
    - Time proximity (30%): How close to resolution?
    - Adverse selection (20%): How badly can you get picked off?
    
    Returns dict with composite score and breakdown.
    """
    question = market.get('question', '')
    
    # 1. Event/Spike risk (50% weight)
    event_analysis = classify_event_type(question)
    spike_risk = event_analysis['spike_risk']
    
    # 2. Time proximity risk (30% weight)
    # Pass known_spike_date when available (e.g. primary day for nomination markets)
    time_risk = calculate_time_proximity_risk(
        market.get('endDate'),
        market.get('knownSpikeDate')  # optional: when the real resolution event occurs
    )
    
    # 3. Adverse selection risk (20% weight)
    adverse_risk = calculate_adverse_selection_risk(market)
    
    # Interaction effect: binary events + short time = multiplicative danger
    # If it's binary AND resolving soon, spike risk amplifies
    if event_analysis['is_binary'] and time_risk > 70:
        spike_risk = min(spike_risk * 1.15, 100)  # 15% amplification
    
    # Weighted composite
    composite = (spike_risk * 0.50) + (time_risk * 0.30) + (adverse_risk * 0.20)
    
    return {
        'composite': round(composite, 1),
        'spike_risk': round(spike_risk, 1),
        'time_risk': time_risk,
        'adverse_selection_risk': round(adverse_risk, 1),
        'event_category': event_analysis['category'],
        'is_binary_event': event_analysis['is_binary']
    }
```

### Risk Interpretation Guide

| Score | Label | Meaning |
|-------|-------|---------|
| 0-25 | **Low** | Gradual event, months away, deep liquidity. Safe for passive LP. |
| 25-45 | **Moderate** | Some spike potential but manageable. Monitor positions. |
| 45-65 | **Elevated** | Meaningful spike risk. Active management recommended. |
| 65-80 | **High** | Binary event or approaching resolution. High reward but real danger. |
| 80-100 | **Extreme** | Imminent resolution or geopolitical binary. Expect to get picked off. |

### Example Risk Assessments

| Market | Spike | Time | Adverse | Composite | Why |
|--------|-------|------|---------|-----------|-----|
| "Starmer resigns by June" (4 months out) | 85 | 8 | 35 | **52** | Binary event but long runway |
| "Starmer resigns by Feb 28" (3 days out) | 98 | 75 | 35 | **79** | Binary + imminent = danger |
| "Fed cuts rates March meeting" (5 weeks) | 65 | 35 | 15 | **46** | Scheduled spike window |
| "US strikes Iran by Feb 28" (0 days) | 98 | 98 | 40 | **86** | Binary + expired = extreme |

**Asset price markets (e.g. "Silver hit $130", "BTC above $150k", "S&P close over 7000") are EXCLUDED from low-risk LP:** classified as `asset_price`, spike risk 72 — one pump/dump can move price violently. Do not include in best low-risk lists.

## Capital Efficiency Calculation

Capital efficiency measures how much reward you earn per dollar of liquidity provided.

> ⚠️ **Note**: `rewardsMinSize` doesn't exist in the API. Estimate minimum capital from pool liquidity.

```python
def calculate_capital_efficiency(market):
    """
    Returns daily rewards per dollar of estimated minimum capital.
    Higher is better.
    """
    rewards = market['clobRewards'][0]
    
    # Use rewardsDailyRate - the actual daily reward in USD
    daily_rate = float(rewards.get('rewardsDailyRate', 0))
    
    # Estimate min capital as 1% of pool liquidity or $100 minimum
    liquidity = float(market.get('liquidity', 0) or 0)
    min_capital = max(liquidity * 0.01, 100)
    
    if min_capital == 0:
        return 0
    
    # Efficiency = daily rewards / estimated min capital
    efficiency = daily_rate / min_capital
    
    return round(efficiency, 4)
```

### Additional Efficiency Metrics

Include supplementary metrics for comprehensive analysis:

```python
def calculate_days_remaining(end_date_str):
    """Calculate days until market resolution."""
    if not end_date_str:
        return 365  # Default to 1 year if unknown
    
    try:
        end_date = datetime.fromisoformat(end_date_str.replace('Z', '+00:00'))
        now = datetime.now(timezone.utc)
        days = (end_date - now).days
        return max(days, 0)
    except:
        return 365

def calculate_metrics(market):
    """
    Calculate all metrics for a market.
    """
    rewards = market['clobRewards'][0]
    days_remaining = calculate_days_remaining(market.get('endDate'))
    
    # Daily reward from API
    daily_rewards = float(rewards.get('rewardsDailyRate', 0))
    
    # Total potential rewards over remaining duration
    total_rewards = daily_rewards * max(days_remaining, 1)
    
    # Spread from market level
    spread = float(market.get('spread', 0.05) or 0.05)
    spread_cents = spread * 100
    
    # Estimate min capital from liquidity
    liquidity = float(market.get('liquidity', 0) or 0)
    min_capital = max(liquidity * 0.01, 100)
    
    # Estimated APY
    if min_capital > 0:
        apy_estimate = (daily_rewards / min_capital) * 365 * 100
    else:
        apy_estimate = 0
    
    return {
        'daily_rewards': round(daily_rewards, 2),
        'total_rewards': round(total_rewards, 2),
        'spread_cents': round(spread_cents, 2),
        'min_capital_estimate': round(min_capital, 2),
        'estimated_apy': round(apy_estimate, 2),
        'days_remaining': days_remaining
    }
```

## Output Format

### JSON Structure

```json
{
  "generated_at": "2026-02-09T14:30:00Z",
  "total_markets": 2743,
  "markets": [
    {
      "question": "Will the Fed decrease interest rates by 25 bps after the March 2026 meeting?",
      "slug": "will-the-fed-decrease-interest-rates-by-25-bps-after-the-march-2026-meeting",
      "daily_rewards": 500.0,
      "total_rewards": 18000.0,
      "min_capital_estimate": 2559.81,
      "spread_cents": 1.0,
      "competitive": 0.8881,
      "volume": 8989264.9,
      "liquidity": 255980.7,
      "end_date": "2026-03-18T00:00:00Z",
      "days_remaining": 36,
      "yes_price": 0.145,
      "no_price": 0.855,
      "risk_score": 48.3,
      "risk_breakdown": {
        "spike_risk": 65.0,
        "time_risk": 35,
        "adverse_selection_risk": 32.5,
        "event_category": "scheduled",
        "is_binary_event": false
      },
      "capital_efficiency": 0.1953,
      "estimated_apy": 7129.44,
      "url": "https://polymarket.com/event/will-the-fed-decrease-interest-rates-by-25-bps-after-the-march-2026-meeting"
    },
    {
      "question": "Starmer out by February 28, 2026?",
      "slug": "starmer-out-by-february-28-2026",
      "daily_rewards": 500.0,
      "total_rewards": 9500.0,
      "min_capital_estimate": 1139.0,
      "spread_cents": 0.2,
      "competitive": 0.9002,
      "volume": 2095949.7,
      "liquidity": 113900.2,
      "end_date": "2026-02-28T00:00:00Z",
      "days_remaining": 19,
      "yes_price": 0.167,
      "no_price": 0.833,
      "risk_score": 72.4,
      "risk_breakdown": {
        "spike_risk": 97.75,
        "time_risk": 55,
        "adverse_selection_risk": 38.7,
        "event_category": "binary",
        "is_binary_event": true
      },
      "capital_efficiency": 0.439,
      "estimated_apy": 16022.8,
      "url": "https://polymarket.com/event/starmer-out-by-february-28-2026"
    }
  ],
  "summary": {
    "avg_risk_score": 51.0,
    "avg_capital_efficiency": 0.0538,
    "total_daily_rewards": 20722.0,
    "markets_by_risk": {
      "low_risk": 166,
      "medium_risk": 2117,
      "high_risk": 460
    },
    "markets_by_event_type": {
      "binary": 892,
      "scheduled": 341,
      "gradual": 1124,
      "unknown": 386
    }
  }
}
```

### CSV Structure

Output columns:
```
question,slug,daily_rewards,total_rewards,spread_cents,volume,liquidity,days_remaining,yes_price,risk_score,capital_efficiency,estimated_apy,url
```

## Implementation Guidelines

### Step-by-Step Execution

When the user requests LP reward analysis:

1. **Fetch markets**: Make API calls to get all active markets (expect 25,000+ markets)
2. **Paginate**: Use offset/limit pagination, 100 markets per request
3. **Filter for rewards**: Keep only markets with `clobRewards` AND `rewardsDailyRate > 0`
4. **Extract core data**: Pull relevant fields for each market
5. **Calculate metrics**: Run risk and efficiency calculations
6. **Exclude asset-price markets** from low-risk recommendations: filter out markets with `event_category == 'asset_price'` (commodity, crypto, stock price bets — one pump can change things a lot)
7. **Sort results**: Order by daily rewards or capital efficiency
8. **Generate output**: Create JSON or CSV based on user preference
9. **Save to file**: Write the structured data to a file in `/mnt/user-data/outputs/`
10. **Provide summary**: Give the user a brief overview of findings (do not recommend asset-price markets as low risk)

### Error Handling

- If API calls fail, retry once, then inform user
- If a market is missing critical fields, skip it and note in summary
- If no markets have rewards, inform user clearly
- Handle string/float type mismatches in `outcomePrices` and other fields

### Performance Considerations

- Cache orderbook data if fetching for multiple tokens (avoid rate limits)
- Implement pagination to handle large numbers of markets (27,000+)
- Consider adding a `limit` parameter if user only wants top N opportunities
- Expect full fetch to take 2-3 minutes due to pagination

## Example Usage

**User:** "Fetch all Polymarket LP reward markets and analyze them for me"

**Assistant response:**
1. Fetches markets from Gamma API with pagination
2. Filters for those with `clobRewards` and `rewardsDailyRate > 0`
3. Calculates risk scores and capital efficiency
4. Generates JSON file with structured data
5. Saves to outputs directory
6. Provides summary: "Found 2,743 markets with LP rewards totaling $20,722/day. Top opportunity: Fed rate markets at $500/day with medium risk (48). Full analysis saved to polymarket_lp_rewards.json"

## Best Practices

1. **Always fetch fresh data**: Market conditions change rapidly
2. **Include timestamps**: User should know when data was fetched
3. **Provide context**: Risk scores are relative, explain what they mean
4. **Link to markets**: Include URLs so user can investigate further
5. **Be honest about limitations**: These are estimates, not guarantees
6. **Consider user's risk tolerance**: Higher efficiency often means higher risk
7. **Filter zero-reward markets**: Many markets have `clobRewards` but `rewardsDailyRate: 0`
8. **Think about the EVENT, not just the numbers**: A "resignation" market at 19 days out is far riskier than a long-duration non–asset-price market at 7 days out. Do not treat asset-price markets (commodity, crypto, stock) as low risk — one pump can change things a lot.
9. **Flag binary events**: Always highlight when an event can resolve instantly on a single headline
10. **Warn on imminent resolution**: Markets resolving in hours are exponentially more dangerous than days

## Common Pitfalls to Avoid

| Pitfall | Solution |
|---------|----------|
| Using `rewardsAmount` instead of `rewardsDailyRate` | Always use `rewardsDailyRate` for daily rewards |
| Looking for `rewardsMinSize` field | Doesn't exist—estimate from pool liquidity |
| Looking for `rewardsMaxSpread` field | Doesn't exist—use market-level `spread` field |
| Not paginating API requests | Markets exceed 25,000—always paginate |
| Treating `outcomePrices` as a list | It's a JSON string—parse it first |
| Treating `spread` as cents | It's a decimal (0.01 = 1 cent) |
| Linear time risk scaling | Use exponential—hours away is FAR riskier than days |
| Ignoring event type | "Resignation" vs non–asset-price events have totally different spike profiles |
| Including asset-price markets in low-risk LP lists | **Exclude them**: commodity, crypto, or stock price bets — one pump/dump can move price violently |
| Same risk for 3 hours vs 3 days | 3 hours = 98 risk, 3 days = 75 risk—massive difference |
| Not flagging binary events | Always warn when single headline can move price 50%+ |
| Treating nomination/election markets as "unknown" | Add nominee, nomination, primary, district pattern (e.g. PA-03) to scheduled triggers |
| Using only market end date for election markets | Use **known spike date** (e.g. primary day) when available—risk should spike as you approach that date |

## Notes

- Polymarket rewards are paid daily at midnight UTC
- Minimum payout is $1; amounts below this are not paid
- Orders must be within the market spread to earn rewards
- Competition levels can change rapidly as more LPs enter markets
- This analysis doesn't account for potential trading PnL (gains/losses from price movements)
- The `competitive` field ranges from 0-1, where higher means more competition

## Changelog

### Feb 2026 Update (v4) — Exclude asset-price markets from low-risk LP
- **BREAKING for low-risk lists**: Betting on the price of an asset (commodity, crypto, stock) is **excluded** from low-risk LP recommendations.
  - New category **`asset_price`** with spike risk **72**: one pump or dump can move price violently (e.g. "Silver hit $130", "BTC above $150k", "S&P close over 7000").
  - **`asset_price_triggers`** in code: silver, gold, bitcoin, btc, eth, crypto, price above/below, stock, s&p, nasdaq, dow, spx, hit $, close over/above/below, etc. Removed these from `gradual_triggers`; gradual is now only non–price milestones (gdp, inflation, subscribers, etc.).
  - Implementation: filter out `event_category == 'asset_price'` when building best low-risk markets; do not recommend these for farming LP with minimal risk.
  - Event table: "Crypto/asset prices" row replaced with "**Asset price markets** | **EXCLUDE**".

### Feb 2026 Update (v3) — Election awareness
- **NEW**: Election/nomination classification
  - Added to **scheduled_triggers**: `nominee`, `nomination`, `primary`, `democratic nominee`, `republican nominee`, `general election`
  - **Congressional district pattern**: regex `\b[A-Z]{2}-\d{1,2}\b` (e.g. PA-03, FL-19) marks market as scheduled (primary/nomination)
  - Event table: new row for **Election/primary/nomination** (scheduled binary—known spike date, outcome unknown until then)
- **NEW**: **Known spike date** for time proximity
  - `calculate_time_proximity_risk(end_date_str, known_spike_date_str=None)`: when `known_spike_date_str` is set (e.g. primary day), risk is based on the **nearer** of end date and spike date so risk spikes as you approach the event
  - `calculate_risk_score` passes optional `market.get('knownSpikeDate')`; implementers can infer or store this for election markets

### Feb 2026 Update (v2)
- **NEW**: Complete rewrite of risk scoring algorithm
  - Added **Spike Risk (50%)**: Classifies events by how suddenly they can resolve
  - Added **Event Classification**: Binary, scheduled, gradual, unknown categories
  - Changed time risk to **exponential scaling** (hours matter more than days)
  - Added **Adverse Selection Risk**: Price extremity, liquidity depth, competition
  - Added interaction effects (binary + imminent = amplified danger)
- New output fields: `spike_risk`, `event_category`, `is_binary_event`, `adverse_selection_risk`
- Added `markets_by_event_type` to summary

### Feb 2026 Update (v1)
- **BREAKING**: Corrected `rewardsDailyRate` as the primary reward field (not `rewardsAmount`)
- **BREAKING**: Removed references to non-existent fields (`rewardsMinSize`, `rewardsMaxSpread`)
- Added documentation for market-level `spread` field
- Added pagination guidance for 27,000+ markets
- Added type handling for `outcomePrices` JSON string
- Added real-world example data from live API

## Future Enhancements

Potential improvements for future versions:
- Historical reward rate tracking
- LP competition analysis (number of active LPs)
- Correlation analysis between markets
- Portfolio optimization (diversification across multiple markets)
- Real-time monitoring and alerts for new high-efficiency opportunities
