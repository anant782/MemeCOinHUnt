"""
BNB Chain Meme Coin Monitor
============================
ROOT CAUSE OF MISSED DIPS (and the fix):
─────────────────────────────────────────
OLD approach (broken):
  - Stored 1-2 local price snapshots, computed peak from those.
  - If the app ran for only 30 min, "peak" = price 30 min ago, NOT the 24h high.
  - Coins like UniFAI showing -34% on CMC were invisible to the monitor.

NEW approach (correct):
  - CoinGecko /coins/markets already returns `price_change_percentage_24h`
    — the REAL 24h change. We store and use it directly.
  - No warm-up period. Dip alerts appear on the very first fetch.
  - Multiple categories are queried so coins not tagged BNB-chain are caught.

FREE/PUBLIC APIs:
  CoinGecko  https://api.coingecko.com/api/v3  (no key)
  PancakeSwap Subgraph  https://api.thegraph.com/subgraphs/...  (free GraphQL)
"""

import os, time, sqlite3, logging, threading, random
from datetime import datetime, timedelta, timezone
from flask import Flask, jsonify, render_template, g
import requests

# ── Config ──────────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH  = os.path.join(DATA_DIR, "coins.db")
os.makedirs(DATA_DIR, exist_ok=True)

DIP_THRESHOLD   = -10.0   # % — coins at or below this are dip alerts
REFRESH_PRICES  = 120     # seconds between full refresh cycles
MAX_HISTORY_PTS = 288

COINGECKO_BASE = "https://api.coingecko.com/api/v3"
PANCAKE_GRAPH  = "https://api.thegraph.com/subgraphs/name/pancakeswap/exchange-v2"

# Multiple categories catch coins like UniFAI not tagged 'binance-smart-chain'
COINGECKO_CATEGORIES = [
    "binance-smart-chain",
    "meme-token",
    "bnb-chain-ecosystem",
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
log = logging.getLogger("bnb-monitor")
app = Flask(__name__)

# ── DB ───────────────────────────────────────────────────────────────────────
def get_db():
    if not hasattr(g, "_db"):
        g._db = sqlite3.connect(DB_PATH, check_same_thread=False)
        g._db.row_factory = sqlite3.Row
    return g._db

def _conn():
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    con = _conn()
    con.executescript("""
        CREATE TABLE IF NOT EXISTS coins (
            address          TEXT PRIMARY KEY,
            name             TEXT NOT NULL,
            symbol           TEXT NOT NULL,
            logo_url         TEXT DEFAULT '',
            source           TEXT DEFAULT 'unknown',
            detected_at      TEXT NOT NULL,
            coingecko_id     TEXT DEFAULT '',
            price_usd        REAL DEFAULT 0,
            price_change_24h REAL DEFAULT 0,
            high_24h         REAL DEFAULT 0,
            low_24h          REAL DEFAULT 0,
            market_cap       REAL DEFAULT 0,
            volume_24h       REAL DEFAULT 0,
            last_updated     TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS price_data (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            address     TEXT NOT NULL,
            price_usd   REAL NOT NULL,
            recorded_at TEXT NOT NULL,
            FOREIGN KEY(address) REFERENCES coins(address)
        );
        CREATE INDEX IF NOT EXISTS idx_pa ON price_data(address);
        CREATE INDEX IF NOT EXISTS idx_pt ON price_data(recorded_at);
    """)
    con.commit(); con.close()
    log.info("DB ready → %s", DB_PATH)

# ── HTTP helpers ─────────────────────────────────────────────────────────────
def _get(url, params=None, timeout=15):
    try:
        r = requests.get(url, params=params,
                         headers={"User-Agent": "BNBMemeMonitor/2.0"}, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning("GET %s → %s", url, e); return None

def _post(url, payload, timeout=15):
    try:
        r = requests.post(url, json=payload, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning("POST %s → %s", url, e); return None

def utcnow():
    return datetime.now(timezone.utc)

# ── CoinGecko fetch (THE KEY FIX) ───────────────────────────────────────────
def fetch_coingecko_category(category, page=1):
    """
    Fetch one page of coins for a CoinGecko category.
    CRITICAL: price_change_percentage=24h makes the API return
    `price_change_percentage_24h_in_currency` — the REAL 24h change %.
    We store this directly; no local-history calculation needed.
    """
    data = _get(f"{COINGECKO_BASE}/coins/markets", params={
        "vs_currency":             "usd",
        "category":                category,
        "order":                   "volume_desc",
        "per_page":                100,
        "page":                    page,
        "sparkline":               "false",
        "price_change_percentage": "24h",
    })
    if not data or not isinstance(data, list):
        return []

    results = []
    for coin in data:
        cid   = coin.get("id", "")
        price = coin.get("current_price") or 0.0
        # Try both field names CoinGecko uses
        chg24 = (
            coin.get("price_change_percentage_24h_in_currency")
            or coin.get("price_change_percentage_24h")
            or 0.0
        )
        if not cid or price == 0:
            continue
        results.append({
            "address":          cid,
            "name":             coin.get("name", "Unknown"),
            "symbol":           (coin.get("symbol") or "???").upper(),
            "logo_url":         coin.get("image", ""),
            "coingecko_id":     cid,
            "price_usd":        price,
            "price_change_24h": round(float(chg24), 4),
            "high_24h":         coin.get("high_24h") or 0.0,
            "low_24h":          coin.get("low_24h")  or 0.0,
            "market_cap":       coin.get("market_cap") or 0.0,
            "volume_24h":       coin.get("total_volume") or 0.0,
            "source":           "coingecko",
        })
    return results

def fetch_all_coingecko():
    seen = {}
    for cat in COINGECKO_CATEGORIES:
        for pg in ([1, 2] if cat == "meme-token" else [1]):
            for c in fetch_coingecko_category(cat, pg):
                cid = c["coingecko_id"]
                if cid not in seen or abs(c["price_change_24h"]) > abs(seen[cid]["price_change_24h"]):
                    seen[cid] = c
            time.sleep(1.2)   # respect free-tier rate limit
    log.info("CoinGecko: %d unique tokens across all categories", len(seen))
    return list(seen.values())

def fetch_pancakeswap():
    data = _post(PANCAKE_GRAPH, {"query": """
    { pairs(first:30, orderBy:createdAtTimestamp, orderDirection:desc) {
        id
        token0 { id name symbol }
        token1 { id name symbol }
        token0Price token1Price
    }}"""})
    if not data or "data" not in data:
        return []
    stable = {"wbnb","bnb","busd","usdt","usdc","dai"}
    results = []
    for pair in data["data"].get("pairs", []):
        t0, t1 = pair["token0"], pair["token1"]
        tok, price = (t1, float(pair.get("token1Price") or 0)) \
            if t0["symbol"].lower() in stable \
            else (t0, float(pair.get("token0Price") or 0))
        if not tok["id"] or not tok["name"]:
            continue
        results.append({
            "address": tok["id"].lower(), "name": tok["name"],
            "symbol": (tok["symbol"] or "???").upper(),
            "logo_url": "", "coingecko_id": "",
            "price_usd": price, "price_change_24h": 0.0,
            "high_24h": 0.0, "low_24h": 0.0,
            "market_cap": 0.0, "volume_24h": 0.0,
            "source": "pancakeswap",
        })
    log.info("PancakeSwap: %d new pairs", len(results))
    return results

def simulate_coins(existing, n=8):
    pool = [
        ("MoonDoge","MDOGE"),("PepeRocket","PRKT"),("BabyShiba","BSHIB"),
        ("TurboFloki","TFLOKI"),("GigaChad","GIGA"),("CumRocket","CUMMIES"),
        ("EverGrow","EGC"),("Kishu Inu","KISHU"),("Volt Inu","VOLT"),
        ("UniFAI Network","UFAI"),("SafeElonV2","SELV2"),("PinkMoon","PINK"),
    ]
    coins = []
    for i,(name,sym) in enumerate(pool):
        addr = f"0xsim{i:04d}{'a'*36}"[:42]
        if addr in existing: continue
        price = round(random.uniform(0.000001,0.05),8)
        chg   = round(random.uniform(-40,30),2)
        coins.append({
            "address":addr,"name":name,"symbol":sym,"logo_url":"","coingecko_id":"",
            "price_usd":price,"price_change_24h":chg,
            "high_24h": price/(1+chg/100) if chg<0 else price*1.1,
            "low_24h":price*0.9,"market_cap":0.0,"volume_24h":0.0,"source":"simulated",
        })
        if len(coins)>=n: break
    return coins

# ── DB writes ─────────────────────────────────────────────────────────────────
def upsert_coin(con, c):
    now = utcnow().isoformat()
    con.execute("""
        INSERT INTO coins
            (address,name,symbol,logo_url,source,detected_at,coingecko_id,
             price_usd,price_change_24h,high_24h,low_24h,market_cap,volume_24h,last_updated)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(address) DO UPDATE SET
            price_usd=excluded.price_usd,
            price_change_24h=excluded.price_change_24h,
            high_24h=excluded.high_24h, low_24h=excluded.low_24h,
            market_cap=excluded.market_cap, volume_24h=excluded.volume_24h,
            last_updated=excluded.last_updated,
            logo_url=CASE WHEN coins.logo_url='' THEN excluded.logo_url ELSE coins.logo_url END,
            name=CASE WHEN coins.name='Unknown' THEN excluded.name ELSE coins.name END
    """, (c["address"],c["name"],c["symbol"],c.get("logo_url",""),
          c.get("source","unknown"),now,c.get("coingecko_id",""),
          c.get("price_usd",0),c.get("price_change_24h",0),
          c.get("high_24h",0),c.get("low_24h",0),
          c.get("market_cap",0),c.get("volume_24h",0),now))

def insert_price(con, address, price):
    con.execute("INSERT INTO price_data(address,price_usd,recorded_at) VALUES(?,?,?)",
                (address, price, utcnow().isoformat()))
    con.execute("""DELETE FROM price_data WHERE id IN (
        SELECT id FROM price_data WHERE address=?
        ORDER BY recorded_at DESC LIMIT -1 OFFSET ?)""", (address, MAX_HISTORY_PTS))

# ── Dip logic ─────────────────────────────────────────────────────────────────
def is_real_dip(chg24: float) -> bool:
    """
    Uses CoinGecko's own 24h change. Accurate from first fetch.
    DIP_THRESHOLD = -10.0 → alert when chg24 <= -10.0
    """
    return chg24 <= DIP_THRESHOLD

def local_dip(con, address):
    """Fallback for PancakeSwap/simulated coins without CoinGecko data."""
    rows = con.execute(
        "SELECT price_usd FROM price_data WHERE address=? ORDER BY recorded_at",
        (address,)
    ).fetchall()
    if len(rows) < 2: return None
    prices = [r["price_usd"] for r in rows]
    peak, current = max(prices), prices[-1]
    if peak == 0: return None
    drop = (peak - current) / peak * 100
    return {"dip_pct": round(drop, 2), "is_dip": drop >= abs(DIP_THRESHOLD), "peak": peak}

# ── Background thread ─────────────────────────────────────────────────────────
def job_refresh():
    """Single thread: fetch all data, upsert, update prices."""
    while True:
        try:
            log.info("[refresh] Cycle start")
            con = _conn()

            all_coins = fetch_all_coingecko() + fetch_pancakeswap()

            # De-duplicate by address
            by_addr = {}
            for c in all_coins:
                if c["address"] not in by_addr:
                    by_addr[c["address"]] = c

            if not by_addr:
                log.warning("[refresh] All APIs down, using simulated data")
                existing = {r[0] for r in con.execute("SELECT address FROM coins")}
                for c in simulate_coins(existing, 10):
                    by_addr[c["address"]] = c

            for c in by_addr.values():
                upsert_coin(con, c)
                if c.get("price_usd", 0) > 0:
                    insert_price(con, c["address"], c["price_usd"])

            con.commit(); con.close()
            log.info("[refresh] %d coins upserted", len(by_addr))

        except Exception as e:
            log.error("[refresh] %s", e, exc_info=True)

        time.sleep(REFRESH_PRICES)

# ── Flask routes ──────────────────────────────────────────────────────────────
@app.teardown_appcontext
def close_db(e): db=g.pop("_db",None); db and db.close()

@app.route("/")
def index(): return render_template("index.html")

@app.route("/api/new_coins")
def api_new_coins():
    con = get_db()
    rows = con.execute("SELECT * FROM coins ORDER BY detected_at DESC").fetchall()
    coins = [{
        "address": r["address"], "name": r["name"], "symbol": r["symbol"],
        "logo_url": r["logo_url"], "source": r["source"],
        "detected_at": r["detected_at"], "coingecko_id": r["coingecko_id"],
        "price_usd": r["price_usd"], "price_change_24h": r["price_change_24h"],
        "high_24h": r["high_24h"], "low_24h": r["low_24h"],
        "volume_24h": r["volume_24h"], "last_updated": r["last_updated"],
    } for r in rows]
    return jsonify({"coins": coins, "total": len(coins)})

@app.route("/api/dip_alerts")
def api_dip_alerts():
    """
    Returns coins with a REAL >= 10% dip.
    For CoinGecko coins: uses price_change_24h (authoritative, same as CMC shows).
    For others: falls back to local price history.
    """
    con = get_db()
    rows = con.execute("SELECT * FROM coins").fetchall()
    alerts = []
    for r in rows:
        chg = r["price_change_24h"]
        src = r["source"]

        if src == "coingecko" and chg != 0:
            if is_real_dip(chg):
                alerts.append({
                    "address": r["address"], "name": r["name"], "symbol": r["symbol"],
                    "logo_url": r["logo_url"], "source": src,
                    "coingecko_id": r["coingecko_id"],
                    "current_price": r["price_usd"], "high_24h": r["high_24h"],
                    "low_24h": r["low_24h"], "volume_24h": r["volume_24h"],
                    "dip_pct": round(abs(chg), 2),
                    "price_change_24h": round(chg, 2),
                    "dip_source": "coingecko_24h",
                })
        else:
            ld = local_dip(con, r["address"])
            if ld and ld["is_dip"]:
                alerts.append({
                    "address": r["address"], "name": r["name"], "symbol": r["symbol"],
                    "logo_url": r["logo_url"], "source": src,
                    "coingecko_id": r["coingecko_id"],
                    "current_price": r["price_usd"], "high_24h": ld["peak"],
                    "low_24h": r["price_usd"], "volume_24h": r["volume_24h"],
                    "dip_pct": round(ld["dip_pct"], 2),
                    "price_change_24h": -round(ld["dip_pct"], 2),
                    "dip_source": "local_history",
                })

    alerts.sort(key=lambda x: x["dip_pct"], reverse=True)
    return jsonify({"alerts": alerts, "total": len(alerts)})

@app.route("/api/price_history/<path:address>")
def api_price_history(address):
    con = get_db()
    coin = con.execute("SELECT * FROM coins WHERE address=?", (address,)).fetchone()
    if not coin:
        return jsonify({"error": "not found"}), 404

    rows = con.execute(
        "SELECT price_usd, recorded_at FROM price_data WHERE address=? ORDER BY recorded_at",
        (address,)
    ).fetchall()
    history = [{"price": r["price_usd"], "time": r["recorded_at"]} for r in rows]

    # Synthesise 48-point history from CoinGecko 24h data if local history is sparse
    if len(history) < 5 and coin["price_usd"] > 0 and coin["price_change_24h"] != 0:
        history = _synth_history(coin["price_usd"], coin["price_change_24h"])

    chg = coin["price_change_24h"]
    return jsonify({
        "address": address, "name": coin["name"], "symbol": coin["symbol"],
        "history": history,
        "dip": {
            "dip_pct": round(abs(chg), 2),
            "price_change_24h": round(chg, 2),
            "is_dip": is_real_dip(chg),
            "high_24h": coin["high_24h"],
            "low_24h": coin["low_24h"],
            "current_price": coin["price_usd"],
        },
        "volume_24h": coin["volume_24h"],
    })

def _synth_history(current, chg_pct):
    """Generate a smooth 48-point price curve from 24h-ago price to now."""
    pts = 48
    now = utcnow()
    start = current / (1 + chg_pct / 100)
    out = []
    for i in range(pts):
        t     = i / (pts - 1)
        ts    = now - timedelta(hours=24*(1-t))
        price = start + (current - start) * t + random.gauss(0, abs(current-start)*0.03)
        out.append({"price": max(price, 1e-12), "time": ts.isoformat()})
    return out

@app.route("/api/stats")
def api_stats():
    con = get_db()
    total  = con.execute("SELECT COUNT(*) FROM coins").fetchone()[0]
    n_dips = con.execute(
        "SELECT COUNT(*) FROM coins WHERE price_change_24h <= ?", (DIP_THRESHOLD,)
    ).fetchone()[0]
    worst  = con.execute(
        "SELECT name,symbol,price_change_24h FROM coins ORDER BY price_change_24h LIMIT 1"
    ).fetchone()
    srcs = con.execute("SELECT source,COUNT(*) n FROM coins GROUP BY source").fetchall()
    return jsonify({
        "total_coins": total, "total_dips": n_dips,
        "worst_dip": dict(worst) if worst else None,
        "sources": {r["source"]: r["n"] for r in srcs},
        "dip_threshold": DIP_THRESHOLD,
        "last_updated": utcnow().isoformat()+"Z",
    })

# ── Boot ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    init_db()
    threading.Thread(target=job_refresh, name="refresh", daemon=True).start()
    log.info("Refresh thread started (every %ds)", REFRESH_PRICES)
    app.run(debug=False, host="0.0.0.0", port=5000)