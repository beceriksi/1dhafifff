# main.py — Pump-safe Multi-Exchange Super Bot (follow-whale enabled)
import os, time, requests, pandas as pd, numpy as np
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

# ========== SETTINGS (env overridable) ==========
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID        = os.getenv("CHAT_ID")

# scan
SCAN_LIMIT_PER_EXCHANGE = int(os.getenv("SCAN_LIMIT_PER_EXCHANGE","200"))

# timeframes
TIMEFRAMES = os.getenv("TIMEFRAMES","1m,5m,15m,1h").split(",")  # pump yakalamak istiyorsan 1m/5m ekle
# detection thresholds
MIN_TURNOVER         = float(os.getenv("MIN_TURNOVER","20000"))     # quoteVolume (USD) min (çok düşük coin'leri azalt)
PUMP_RATIO           = float(os.getenv("PUMP_RATIO","1.8"))         # son bar / EMA(n) >= PUMP_RATIO -> pump candidate
PUMP_RAMP_MIN        = float(os.getenv("PUMP_RAMP_MIN","1.15"))     # son 3 bar toplam / prev avg >= ramp
PUMP_CONFIRM_CANDLES = int(os.getenv("PUMP_CONFIRM_CANDLES","2"))   # kaç mum süren pump teyidi
WHALE_USD            = float(os.getenv("WHALE_USD","800000"))       # tek bar whale threshold
WHALe_RATIO          = float(os.getenv("WHALE_RATIO","1.25"))       # whale ile birlikte daha gevşetilmiş ratio
FOLLOW_WHALES        = os.getenv("FOLLOW_WHALES","true").lower()=="true"

# confidence / spam control
MIN_CONF             = int(os.getenv("MIN_CONF","60"))
MAX_LINES_PER_SIDE   = int(os.getenv("MAX_LINES_PER_SIDE","25"))
COOLDOWN_MINUTES     = int(os.getenv("COOLDOWN_MINUTES","60"))      # öneri: 60 dk; (stateless: per-run etkili değil)

# exchanges endpoints
MEXC      = "https://api.mexc.com"
BINANCE   = "https://api.binance.com"
BYBIT     = "https://api.bybit.com"
BITGET    = "https://api.bitget.com"
COINGECKO = "https://api.coingecko.com/api/v3/global"

# ========== HELPERS ==========
def ts(): return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
def jget(url, params=None, retries=2, timeout=8):
    for _ in range(retries):
        try:
            r = requests.get(url, params=params, timeout=timeout)
            if r.status_code == 200:
                return r.json()
        except:
            time.sleep(0.25)
    return None

def telegram(text):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print(text); return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      json={"chat_id":CHAT_ID,"text":text,"parse_mode":"Markdown"}, timeout=15)
    except Exception as e:
        print("telegram err", e)

# indicators
def ema(series, n): return series.ewm(span=n, adjust=False).mean()
def rsi(s, n=14):
    d=s.diff(); up=d.clip(lower=0); dn=-d.clip(upper=0)
    rs = up.ewm(alpha=1/n, adjust=False).mean() / (dn.ewm(alpha=1/n, adjust=False).mean()+1e-12)
    return 100-(100/(1+rs))
def volume_ratio(turnover, n=10):
    base = turnover.ewm(span=n, adjust=False).mean()
    return float(turnover.iloc[-1] / (base.iloc[-2] + 1e-12))

# ========== COIN LIST (multi-exchange, fallback chain) ==========
def mexc_symbols(limit=200):
    d=jget(f"{MEXC}/api/v3/ticker/24hr")
    if not d: return []
    rows=[x for x in d if x.get("symbol","").endswith("USDT")]
    rows.sort(key=lambda x: float(x.get("quoteVolume","0")), reverse=True)
    return [x["symbol"] for x in rows[:limit]]

def binance_symbols(limit=200):
    d=jget(f"{BINANCE}/api/v3/ticker/24hr")
    if not d: return []
    rows=[x for x in d if x.get("symbol","").endswith("USDT")]
    rows.sort(key=lambda x: float(x.get("quoteVolume","0")), reverse=True)
    return [x["symbol"] for x in rows[:limit]]

def bybit_symbols(limit=200):
    d=jget(f"{BYBIT}/v2/public/tickers")
    if not d or "result" not in d: return []
    rows=[x for x in d["result"] if x.get("symbol","").endswith("USDT")]
    rows.sort(key=lambda x: float(x.get("quoteVolume","0")), reverse=True)
    return [x["symbol"] for x in rows[:limit]]

def bitget_symbols(limit=200):
    # bitget spot markets endpoint (public)
    d=jget(f"https://api.bitget.com/api/spot/v1/public/products")
    if not d or "data" not in d: return []
    rows=[x for x in d["data"] if x.get("quoteCurrency","").upper()=="USDT"]
    rows.sort(key=lambda x: float(x.get("quoteVolume24h","0")), reverse=True)
    return [f"{r['baseCurrency'].upper()}{r['quoteCurrency'].upper()}" for r in rows[:limit]]

# ========== KLINES (per-exchange wrappers) ==========
def klines_binance(sym, interval, limit=200):
    d=jget(f"{BINANCE}/api/v3/klines", {"symbol":sym,"interval":interval,"limit":limit})
    if not d: return None
    df=pd.DataFrame(d, columns=list(range(len(d[0]))))
    df = df.astype(float)
    # columns: 0=openTime,4=close,7=quoteVolume
    return pd.DataFrame({"close":df[4],"high":df[2],"low":df[3],"turnover":df[7]})

def klines_mexc(sym, interval, limit=200):
    d=jget(f"{MEXC}/api/v3/klines", {"symbol":sym,"interval":interval,"limit":limit})
    if not d: return None
    df=pd.DataFrame(d, columns=list(range(len(d[0]))))
    df = df.astype(float)
    # MEXC: close at index 4, qv (quote volume) index 6
    return pd.DataFrame({"close":df[4],"high":df[2],"low":df[3],"turnover":df[6]})

def klines_bybit(sym, interval, limit=200):
    # bybit uses interval like "1" for 1m, "60" for 1h etc. try public endpoint
    mapping = {"1m":"1","5m":"5","15m":"15","1h":"60","4h":"240","1d":"D"}
    p = mapping.get(interval, interval)
    d=jget(f"https://api.bybit.com/public/linear/kline", {"symbol":sym,"interval":p,"limit":limit})
    if not d or "result" not in d: return None
    arr=d["result"]
    df=pd.DataFrame(arr)
    df=df.astype(float)
    return pd.DataFrame({"close":df["close"],"high":df["high"],"low":df["low"],"turnover":df.get("quoteVolume", df.get("volume", pd.Series([0]*len(df))))})

def klines_bitget(sym, interval, limit=200):
    d=jget(f"https://api.bitget.com/api/spot/v1/market/candles", {"symbol":sym,"period":interval,"limit":limit})
    if not d or "data" not in d: return None
    arr=d["data"]
    df=pd.DataFrame(arr).astype(float)
    # bitget may reverse order; try to standardize:
    return pd.DataFrame({"close":df[4],"high":df[2],"low":df[3],"turnover":df[6]})

# choose best avail kline function
KL_FETCHERS = {
    "BINANCE": klines_binance,
    "MEXC": klines_mexc,
    "BYBIT": klines_bybit,
    "BITGET": klines_bitget
}

# ========== ANALYSIS LOGIC ==========
def analyze_one(source, symbol, interval):
    fetch = KL_FETCHERS.get(source)
    if not fetch: return None
    df = fetch(symbol, interval, 200)
    if df is None or len(df) < max(10, PUMP_CONFIRM_CANDLES+2): return None
    # liquidity
    if float(df["turnover"].iloc[-1]) < MIN_TURNOVER: return None

    c = df["close"]; t = df["turnover"]
    # indicators
    r = float(rsi(c).iloc[-1])
    ratio = volume_ratio(t, n=10)
    ema20 = ema(c,20).iloc[-1]; ema50 = ema(c,50).iloc[-1]
    trend_up = ema20 > ema50
    adx_val = 0
    try:
        # lightweight adx approx via price ranges
        highs = df["high"]; lows = df["low"]
        atr = (highs - lows).ewm(span=14, adjust=False).mean().iloc[-1]
        adx_val = float((abs(ema20-ema50)/ (atr+1e-12)) * 10)
    except: adx_val = 10

    # whale detection
    last_turn = float(t.iloc[-1])
    is_whale = last_turn >= WHALE_USD

    # ramp check: last 3 bars sum vs rolling avg
    ramp = float(t.iloc[-3:].sum() / (t.ewm(span=10,adjust=False).mean().iloc[-2]*3 + 1e-12))

    # pump detection: require either high ratio OR whale+lower ratio
    pump_hit = False
    if ratio >= PUMP_RATIO and ramp >= PUMP_RAMP_MIN:
        pump_hit = True
    elif is_whale and (ratio >= WHALe_RATIO or ramp >= PUMP_RAMP_MIN):
        pump_hit = True

    # confirmation: require pump to persist PUMP_CONFIRM_CANDLES (last bars >= baseline fraction)
    if pump_hit:
        # require at least PUMP_CONFIRM_CANDLES of elevated volume
        elevated = 0
        base = t.ewm(span=10,adjust=False).mean()
        for i in range(1, PUMP_CONFIRM_CANDLES+1):
            if t.iloc[-i] >= 1.0 * base.iloc[-i-1]: elevated += 1
        if elevated < max(1, PUMP_CONFIRM_CANDLES):
            pump_hit = False

    side = None
    # strategy:
    # - pump_hit + (trend_up OR FOLLOW_WHALES) -> BUY
    # - else if not trend_up and ratio <= 0.9 -> SELL
    if pump_hit and (trend_up or (is_whale and FOLLOW_WHALES)):
        side = "BUY"
    elif (not trend_up) and ratio <= float(os.getenv("VOL_RATIO_SELL","0.90")):
        side = "SELL"
    else:
        return None

    # confidence scoring (0-100)
    conf = int(min(100, (ratio*25) + (adx_val/3) + (r/5) + (10 if is_whale else 0) + (10 if pump_hit else 0)))
    if conf < MIN_CONF:
        return None

    return {
        "ex": source,
        "symbol": symbol,
        "tf": interval,
        "side": side,
        "ratio": ratio,
        "rsi": r,
        "adx": adx_val,
        "trend": "↑" if trend_up else "↓",
        "conf": conf,
        "is_whale": bool(is_whale),
        "ramp": ramp
    }

# ========== MAIN ==========
def main():
    start = time.time()
    # fetch coins from all exchanges (merge unique)
    mex = mexc_symbols(SCAN_LIMIT_PER_EXCHANGE)
    bnb = binance_symbols(SCAN_LIMIT_PER_EXCHANGE)
    try:
        byd = bybit_symbols(SCAN_LIMIT_PER_EXCHANGE)
    except: byd=[]
    try:
        bit = bitget_symbols(SCAN_LIMIT_PER_EXCHANGE)
    except: bit=[]
    sources = []
    # prioritize MEXC then BINANCE then BYBIT then BITGET (but include all)
    for s in mex: sources.append(("MEXC", s))
    for s in bnb:
        if ("BINANCE", s) not in sources: sources.append(("BINANCE", s))
    for s in byd:
        if ("BYBIT", s) not in sources: sources.append(("BYBIT", s))
    for s in bit:
        if ("BITGET", s) not in sources: sources.append(("BITGET", s))

    if not sources:
        telegram("⛔ Hiç sembol alınamadı (tüm borsalar)."); return

    results = []
    with ThreadPoolExecutor(max_workers=12) as ex:
        futures = [ex.submit(analyze_one, src, sym, tf) for (src,sym) in sources for tf in TIMEFRAMES]
        for f in as_completed(futures):
            try:
                r=f.result()
                if r: results.append(r)
            except: pass

    buys = [x for x in results if x["side"]=="BUY"]
    sells = [x for x in results if x["side"]=="SELL"]

    # sort and limit
    buys = sorted(buys, key=lambda z:z["conf"], reverse=True)[:MAX_LINES_PER_SIDE]
    sells = sorted(sells, key=lambda z:z["conf"], reverse=True)[:MAX_LINES_PER_SIDE]

    # Build message (single message)
    header = [
        f"⚡ *SUPER BOT — Pump-safe Multi-Exchange*",
        f"⏱ {ts()} | Süre: {int(time.time()-start)}s",
        f"🔎 Sources: MEXC {len(mex)} | BNB {len(bnb)} | BYB {len(byd)} | BITG {len(bit)}",
        f"⚙️ MIN_CONF={MIN_CONF} | PUMP_RATIO={PUMP_RATIO} | WHALE={int(WHALE_USD)} USD",
        ""
    ]
    lines = header
    if buys or sells:
        lines.append("*📈 SİNYALLER*")
        if buys:
            lines.append("🟢 *BUY*")
            for x in buys:
                whale_tag = " 🐳" if x["is_whale"] else ""
                lines.append(f"- [{x['ex']}] {x['symbol']} | {x['tf']} | Conf:{x['conf']} | RSI:{x['rsi']:.1f} | Vol x{x['ratio']:.2f} | ramp:{x['ramp']:.2f}{whale_tag}")
        if sells:
            lines.append("🔴 *SELL*")
            for x in sells:
                lines.append(f"- [{x['ex']}] {x['symbol']} | {x['tf']} | Conf:{x['conf']} | RSI:{x['rsi']:.1f} | Vol x{x['ratio']:.2f} | ramp:{x['ramp']:.2f}")
    else:
        lines.append("ℹ️ Kriterlere uyan sinyal yok. (Ağır filtre / düşük hacim)")

    telegram("\n".join(lines))

if __name__ == "__main__":
    main()
