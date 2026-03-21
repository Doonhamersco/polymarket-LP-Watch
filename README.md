# LPWatch — Polymarket LP scanner & Telegram position monitor

LPWatch helps you farm **low-risk LP rewards** on Polymarket and avoid getting **picked off** on your bids.

- **Scanner**: Finds markets with LP rewards, scores them by risk (spike, time, adverse selection), and shows the **best low-risk opportunities** with volume, liquidity, and reasoning.
- **LP monitor**: Watches your positions and **alerts in Telegram on every poll** while total **USD of bids at or above your limit** (same signal as “bids before”) stays below **$1.2M** (warning band) or below your **min bid depth** (default $50k, critical). Distance to limit is shown in the terminal only.
- **Telegram commands**: Manage positions from Telegram — `/positions`, `/out_of_range`, `/market`, **`/list_event`** (all sub-markets for a game/event), `/add_position`, `/edit_position`, `/bulk_add`, `/remove_position`.

**Note:** The script is self-contained and does not read any files at runtime. You only need `best_lp_markets.py` (and optionally the example configs) to run it. `lp.md` is optional reference documentation for the risk methodology.

---

## Features

### Low-risk LP scanner (mode 1)

- Fetches all active Polymarket markets with LP rewards.
- Risk score (0–100): **spike risk (50%)**, **time risk (30%)**, **adverse selection (20%)**.
- Filters to minimal-risk markets; excludes **asset-price bets** (crypto/commodities/stocks) from low-risk lists.
- Ranks by **capital efficiency**; shows question, risk breakdown, days left, min capital, APY, **total volume**, **liquidity**, and a short reasoning paragraph.
- Color-coded terminal: green/yellow/red by risk; distance > 5¢ labeled **OUT OF RANGE** in red.

### LP position monitor (modes 2 & 3)

- Watches your positions; **Telegram alerts every poll** while **bids before** (total USD at or above your limit, including size at your price) stays **below $1.2M** (warning) or **below `min_bid_depth_usd`** (default $50,000 USD, critical — you get the critical message only, not a separate $1.2M ping). Terminal still shows distance to your limit.
- Sorts by **soonest game first** (shortest time until tip-off), then distance to limit, then **bids before**. Unknown game times and already-started games appear after upcoming games.
- Shows **question**, side, current, limit, distance, and **bids before (at/above limit)** per position (no separate “at limit” column). In the terminal, that dollar amount is **red** when below **$1.2M**, and **red with ⚠** when below your **min bid depth** alert (default $50k). Telegram `/positions` appends ⚠️ under $1.2M.
- Distance colors: ≤1¢ red, ≤2¢ amber, 2–4.9¢ green, **≥5¢ red + OUT OF RANGE**.
- **Game countdown** (sports): tip-off time comes from Polymarket’s API (`gameStartTime` / event times, not web scraping). Each line ends with e.g. `6 HOURS UNTIL GAME`: **green** ≥7h before tip, **orange** 4–7h, **red** &lt;4h or after start — use red as your cue to exit ≥4h before the game.

### Sports & event markets

Polymarket sports games are usually **one event** with **many markets** (moneyline, spreads, totals, alt lines). LPWatch is built to work well with that layout:

| What | How |
|------|-----|
| **See every market for a game** | In Telegram (monitor **must be running**): `/list_event <event-slug-or-url>` — lists all sub-market **slugs** and titles for that event (same data Gamma uses for the event page). Copy a slug into `/add_position …`. Alias: `/event`. |
| **Custom “critical” bid depth** | Edit **`monitor_config.json`** → `settings` → **`min_bid_depth_usd`** (USD). That’s the threshold for the **⚠ LOW BID DEPTH** Telegram alert and the **⚠** marker in the terminal when bids-at/above-limit fall below it. Default is **50,000**. You can set e.g. `25000` or `100000` without changing code. |
| **$1.2M warning band** | The **second** alert tier (“below $1.2M”) and terminal **red** styling use a **fixed** cutoff (`BIDS_BEFORE_DISPLAY_RED_USD` = **1.2M** in `best_lp_markets.py`). Only **`min_bid_depth_usd`** is configurable in JSON. |
| **Poll speed** | Same file: **`poll_interval_seconds`** (default **25**) — how often the monitor refreshes and can send Telegram alerts. |
| **Bulk NCAA men’s CBB slugs** | Mode **[5]** exports all active **NCAA CBB** sub-markets (`series_id` for `ncaa-cbb`) to `cbb_markets_export.tsv` + `cbb_market_slugs.txt` for spreadsheet / scripted bulk adds — alternative to hand-copying from `/list_event` per URL. |
| **Test Telegram** | `python3 best_lp_markets.py --telegram-test` (or `-t`) — one message using saved `monitor_config.json` (checks delivery / desktop notifications). |

**Wallet sync** (optional, in `monitor_config.json`): see [Telegram bot](#telegram-bot) — useful if you trade **filled** sports positions from the app; unfilled LP-only limits still need `positions.json` / Telegram.

### Telegram bot

- **Positions** stored in `positions.json`; **Telegram + settings** in `monitor_config.json` (both created on first run; do not commit these).
- **Auto-sync from wallet** (optional): set `wallet_address` to your Polymarket proxy wallet (`0x…`) and `"sync_positions_from_wallet": true` in `monitor_config.json` settings. The monitor then **reloads positions every poll** from the public **Data API** (same as mode 4). When you **sell or close** a holding, it drops off the terminal on the next refresh — no manual `/remove_position`. **Caveat:** this only sees **filled** holdings (and uses **avg entry** as the reference price for depth/distance). **Unfilled LP limit orders** are not returned by the Data API; keep using `positions.json` / Telegram for those, or use Polymarket’s CLOB authenticated API (not implemented here).
- Commands (commands only work while the **monitor process is running** — it polls Telegram in the same loop as bid-depth checks):
  - `/positions` — list all positions (same format as terminal, sorted by risk).
  - `/out_of_range` — list only positions with **distance ≥ 5¢** (quick way to update stale limits).
  - `/market <SLUG|URL>` — show only the positions you hold in that specific market.
  - **`/list_event <SLUG|URL>`** (alias **`/event`**) — list **all sub-markets** for a sports **event** (moneyline, spreads, totals, props): slug + short title + YES/NO prices when available. Paste an **event** URL from polymarket.com or the **event slug** (e.g. from the URL path). Use this to find the exact **market slug** for each line before `/add_position`.
  - `/add_position <SLUG|URL> <YES/NO> <PRICE>` — add or **update** a position; if a position with the same market+side already exists, its price is replaced.
  - `/edit_position <INDEX> <NEW_PRICE>` — change limit of existing position.
  - `/bulk_add` — next message: many lines `<SLUG|URL> <YES/NO> <PRICE>`.
  - `/remove_position <INDEX> [INDEX ...]` — remove one or several.
  - `/help` — show commands.

---

## Requirements

- **Python 3.10+** (stdlib only for core script; no pip required).
- A **Telegram bot** (via [BotFather](https://t.me/BotFather)) and your **chat_id** for alerts.

---

## Setup

1. **Clone the repo**

   ```bash
   git clone https://github.com/YOUR_USERNAME/lpwatch.git
   cd lpwatch
   ```

2. **(Optional) Virtualenv**

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate   # Windows: .venv\Scripts\activate
   ```

3. **Telegram bot**

   - In Telegram: [@BotFather](https://t.me/BotFather) → `/newbot` → follow prompts; copy **bot token**.
   - Start a chat with your bot, send any message.
   - Get **chat_id**: open `https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates` and find `"chat": { "id": ... }`.

4. **(Optional) BotFather commands**

   In BotFather, `/setcommands` for your bot, then paste:

   ```
   positions - List current positions (by risk)
   out_of_range - List only OUT OF RANGE positions (≥5¢)
   market - Show positions for one market: <SLUG|URL>
   list_event - All sub-markets for an event: <SLUG|URL>
   add_position - Add one: <SLUG> <YES/NO> <PRICE>
   edit_position - Edit limit: <INDEX> <NEW_PRICE>
   bulk_add - Next message: lines of <SLUG> <YES/NO> <PRICE>
   remove_position - Remove by index (or several)
   help - Show commands
   ```

---

## Usage

```bash
python3 best_lp_markets.py
```

**Menu:**

- **[1]** Scan low-risk LP markets only.
- **[2]** Monitor my LP positions (load/save positions + Telegram config, then run monitor).
- **[3]** Scan first, then monitor.
- **[4]** Show my on-chain Polymarket positions by address (read-only, no private key).
- **[5]** **Export all active NCAA men’s CBB markets** — paginates Polymarket’s Gamma `series_id=10470` (same league as `/sports` → `cbb` / `ncaa-cbb`), then for each game fetches every sub-market (moneyline, spreads, totals, …). Writes `cbb_markets_export.tsv` and `cbb_market_slugs.txt` next to the script so you can bulk-add positions without pasting `/list_event` per URL.

On first run in mode 2 or 3 you’ll be prompted for positions (slug/URL, side, limit price) and Telegram token + chat_id; these are saved to `positions.json` and `monitor_config.json`. On later runs you can accept saved config and go straight to monitoring. See `positions.example.json` and `monitor_config.example.json` for the expected format (do not commit real tokens or private data).

**Tuning bid depth & polling:** After the first run, open **`monitor_config.json`** and adjust **`min_bid_depth_usd`** (critical alert threshold in USD) and **`poll_interval_seconds`** without re-running the wizard. For sports, use **`/list_event`** from Telegram (with the monitor running) to discover sub-market slugs, or mode **5** for a full NCAA CBB slug export.

**Example in Telegram:**

```
/positions
/out_of_range
/edit_position 5 0.32
/remove_position 3 7 9
```

---

## Risk model

Risk scoring and event classification (binary, scheduled, election/primary, gradual, asset_price) follow the **Polymarket LP Rewards Analyzer** methodology. Asset-price markets are excluded from low-risk recommendations. See `lp.md` in the repo for full detail.

---

## Disclaimer

This tool is for **research and monitoring only**. No guarantee of profitability or safety; markets can move sharply and liquidity can vanish. Use at your own risk and size positions appropriately.
