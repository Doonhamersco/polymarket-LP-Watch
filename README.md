# LPWatch — Polymarket LP scanner & Telegram position monitor

LPWatch helps you farm **low-risk LP rewards** on Polymarket and avoid getting **picked off** on your bids.

- **Scanner**: Finds markets with LP rewards, scores them by risk (spike, time, adverse selection), and shows the **best low-risk opportunities** with volume, liquidity, and reasoning.
- **LP monitor**: Watches your positions and **alerts in Telegram** when price gets close to your bids, so you can pull or adjust before getting filled.
- **Telegram commands**: Manage positions from Telegram — `/positions`, `/out_of_range`, `/add_position`, `/edit_position`, `/bulk_add`, `/remove_position`.

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

- Watches your positions; alerts when **price nears your limit** (default: 1.0¢, configurable).
- Sorts by **riskiness**: smallest distance first, then fewest **bids before** (dollars of bids at or above your limit).
- Shows **question**, side, current, limit, distance, and **bids before** per position.
- Distance colors: ≤1¢ red, ≤2¢ amber, 2–4.9¢ green, **≥5¢ red + OUT OF RANGE**.

### Telegram bot

- **Positions** stored in `positions.json`; **Telegram + settings** in `monitor_config.json` (both created on first run; do not commit these).
- Commands:
  - `/positions` — list all positions (same format as terminal, sorted by risk).
  - `/out_of_range` — list only positions with **distance ≥ 5¢** (quick way to update stale limits).
  - `/market <SLUG|URL>` — show only the positions you hold in that specific market.
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

**Note:** When monitoring positions (modes 2/3), the script automatically checks for "Up or Down" markets (crypto or stock indices like SPX) starting within 1.5 hours and sends Telegram alerts when new opportunities appear. These markets offer massive rewards ($500–$1000 daily) with zero risk until the underlying market opens.

On first run in mode 2 or 3 you’ll be prompted for positions (slug/URL, side, limit price) and Telegram token + chat_id; these are saved to `positions.json` and `monitor_config.json`. On later runs you can accept saved config and go straight to monitoring. See `positions.example.json` and `monitor_config.example.json` for the expected format (do not commit real tokens or private data).

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
