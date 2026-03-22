# BNB Chain Meme Coin Monitor

A real-time web dashboard that tracks new meme coins on BNB Chain and alerts you
to price dips — using **only free/public APIs, no paid keys required**.

---

## Quick Start

```bash
# 1. Clone / download the project folder
cd bnb_meme_monitor

# 2. Create a virtual environment (recommended)
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Run the app
python app.py
```

Then open **http://localhost:5000** in your browser.

The dashboard auto-refreshes every 10 seconds.
Background threads scan for new coins every 5 minutes and update prices every 3 minutes.

---

## How It Works

### 1. Token Detection

New meme coins are discovered from two free sources, run in a background thread:

| Source | Endpoint | Details |
|--------|----------|---------|
| **CoinGecko** | `/coins/markets?category=binance-smart-chain` | Top 50 BNB tokens by volume, no API key needed |
| **PancakeSwap Subgraph** | The Graph GraphQL API | Newest pairs sorted by `createdAtTimestamp`, completely free |

If both sources are rate-limited or unreachable, a small set of **simulated coins**
(with realistic names/symbols) is generated so the UI always has data to display.

### 2. Price Dip Detection

Every 3 minutes the price-update thread:

1. Fetches current USD prices from CoinGecko for all coins that have a `coingecko_id`.
2. For PancakeSwap / simulated coins it applies a **±8–12 % random walk** to the last
   stored price (realistic for highly volatile meme coins when no live feed exists).
3. Stores the new price point in SQLite.
4. The `/api/dip_alerts` endpoint then compares **current price vs. 24-hour peak**:

```
dip % = (peak_price - current_price) / peak_price × 100
```

Coins where `dip % ≥ 10 %` appear in the **Dip Alerts** panel.

### 3. Data Storage

All data lives in `data/coins.db` (SQLite):

```
coins          — address, name, symbol, logo_url, source, detected_at, coingecko_id
price_data     — id, address, price_usd, recorded_at
```

- Duplicate addresses are automatically ignored (`INSERT OR IGNORE`).
- Price history is capped at **288 points per coin** (≈ 24 hours at 5-minute intervals).
- No external database server needed — SQLite is built into Python.

### 4. Web UI Generation

Flask serves `templates/index.html`.  The page:

- Polls `/api/new_coins` and `/api/dip_alerts` every **10 seconds** via `fetch()`.
- Renders coin cards dynamically in JavaScript.
- Opens a **Chart.js modal** on click, pulling `/api/price_history/<address>`.
- Features a live ticker tape, countdown ring, and skeleton loaders.

---

## API Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /` | Serves the main dashboard |
| `GET /api/new_coins` | All tracked coins (newest first) with latest price |
| `GET /api/dip_alerts` | Coins with ≥10 % dip from 24 h peak (biggest dip first) |
| `GET /api/price_history/<address>` | Full price history + dip stats for one coin |
| `GET /api/stats` | Summary counts (total coins, prices, sources) |

---

## Free/Public APIs Used

| API | URL | Notes |
|-----|-----|-------|
| CoinGecko Public | `https://api.coingecko.com/api/v3` | No key; ~10–30 req/min free tier |
| PancakeSwap Subgraph | `https://api.thegraph.com/subgraphs/name/pancakeswap/exchange-v2` | Free GraphQL |
| BSC Public RPC | `https://bsc-dataseed.binance.org` | Free JSON-RPC (optional, for Web3.py) |

---

## Project Structure

```
bnb_meme_monitor/
├── app.py              # Flask app + background threads + all API logic
├── requirements.txt    # Python dependencies
├── README.md           # This file
├── data/               # Created automatically on first run
│   └── coins.db        # SQLite database
└── templates/
    └── index.html      # Single-page dashboard UI
```

---

## Extending the Project

- **Real on-chain detection**: Uncomment `web3` in requirements.txt and use
  `web3.eth.get_logs()` on the PancakeSwap Factory contract to catch
  `PairCreated` events as they happen (requires a BSC node or public RPC).
- **Telegram alerts**: Add a bot message in `job_update_prices()` whenever a new
  dip is detected.
- **Lower dip threshold**: Change `DIP_THRESHOLD = 0.10` to `0.05` for 5 % alerts.
- **More coins**: Increase the `per_page` param in `fetch_coingecko_bnb_tokens()`.
