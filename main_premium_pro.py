import os
import time
import requests
from datetime import datetime, timezone

# === BASE URL'LER ===
OKX_BASE = "https://www.okx.com"
COINGECKO = "https://api.coingecko.com/api/v3"

# === TELEGRAM ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# === GENEL PARAMETRELER ===
TOP_LIMIT = 150          # En çok hacimli 150 spot USDT coini
CANDLE_LIMIT_4H = 200
CANDLE_LIMIT_1H = 200
TRADES_LIMIT = 200
ORDERBOOK_DEPTH = 20

# Delta ve orderbook eşikleri (global)
NET_DELTA_MIN_POS = 50_000    # Long için minimum net alış delta
NET_DELTA_MIN_NEG = -50_000   # Short için minimum net satış delta
ORDERBOOK_IMB_RATIO = 1.3     # Bid/Ask notional dengesizliği oranı

# Fiyat yapısı parametreleri (4H)
STRUCT_LOOKBACK_4H = 20
ZONE_BUFFER = 0.002  # %0.2

# Strateji modu: 4 koşuldan en az 3'ü sağlamalı
MIN_CONDITIONS_STRICT = 3

# 4H mum kapanışı sonrası sinyal için izin verilen maksimum yaş (ms)
MAX_4H_AGE_MS = 90 * 60 * 1000  # 90 dakika

# MCAP cache (CoinGecko)
MCAP_CACHE = {}  # "BTC" -> market_cap (USD)


# ========== YARDIMCI FONKSİYONLAR ==========

def ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def okx_jget(path, params=None, retries=3, timeout=10):
    """
    OKX için JSON getter (code == 0 ve data alanını döndürür).
    """
    url = path if path.startswith("http") else OKX_BASE + path
    for _ in range(retries):
        try:
            r = requests.get(url, params=params, timeout=timeout)
            if r.status_code == 200:
                j = r.json()
                if isinstance(j, dict) and j.get("code") == "0" and j.get("data"):
                    return j["data"]
        except Exception:
            time.sleep(0.5)
    return None


def http_get_json(url, params=None, retries=3, timeout=10):
    """
    Genel amaçlı JSON GET (CoinGecko vs. için).
    """
    for _ in range(retries):
        try:
            r = requests.get(url, params=params, timeout=timeout)
            if r.status_code == 200:
                return r.json()
        except Exception:
            time.sleep(0.5)
    return None


def telegram(msg: str):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("⚠ TELEGRAM_TOKEN veya CHAT_ID yok, mesaj sadece console'a yazıldı:")
        print(msg)
        print("---------------------")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"}
    try:
        r = requests.post(url, data=payload, timeout=10)
        if r.status_code != 200:
            print("Telegram hata:", r.text)
    except Exception as e:
        print("Telegram exception:", e)


# ========== MCAP & SEGMENT ==========

def build_mcap_cache(max_pages: int = 3):
    """
    CoinGecko üzerinden top marketcap coinleri çekip sembol -> mcap map'i oluşturur.
    BTC, ETH, SOL, XRP vs. kesin bulunur.
    """
    global MCAP_CACHE
    MCAP_CACHE = {}
    for page in range(1, max_pages + 1):
        data = http_get_json(
            f"{COINGECKO}/coins/markets",
            params={
                "vs_currency": "usd",
                "order": "market_cap_desc",
                "per_page": 250,
                "page": page,
                "sparkline": "false",
            },
        )
        if not data:
            break
        for row in data:
            sym = (row.get("symbol") or "").upper()
            mcap = row.get("market_cap") or 0
            if sym and mcap:
                # Aynı sembol birden fazla zincirde olabilir → en yüksek mcap'i al
                if sym not in MCAP_CACHE or mcap > MCAP_CACHE[sym]:
                    MCAP_CACHE[sym] = mcap
        if len(data) < 250:
            break


def get_mcap_segment(base_symbol: str):
    """
    Sembol (örn: BTC) için marketcap segmenti ve whale eşikleri döner.
    Dönen:
      (segment_code, segment_label, s_whale, m_whale, x_whale)
    """
    sym = base_symbol.upper()
    mcap = MCAP_CACHE.get(sym, 0)

    # DEFAULT low/unknown
    segment = "Unknown"
    label = "⬜ Unknown-cap"
    s_whale = 100_000
    m_whale = 150_000
    x_whale = 300_000

    if mcap <= 0:
        return segment, label, s_whale, m_whale, x_whale

    if mcap >= 10_000_000_000:  # 10B+
        segment = "High"
        label = "🟦 High-cap"
        s_whale = 500_000
        m_whale = 1_000_000
        x_whale = 1_500_000
    elif mcap >= 1_000_000_000:  # 1B–10B
        segment = "Mid"
        label = "🟧 Mid-cap"
        s_whale = 200_000
        m_whale = 400_000
        x_whale = 800_000
    elif mcap >= 100_000_000:  # 100M–1B
        segment = "Low"
        label = "🟨 Low-cap"
        s_whale = 100_000
        m_whale = 150_000
        x_whale = 300_000
    else:  # Micro
        segment = "Micro"
        label = "🟨 Low-cap"
        s_whale = 80_000
        m_whale = 120_000
        x_whale = 200_000

    return segment, label, s_whale, m_whale, x_whale


# ========== OKX PİYASA FONKSİYONLARI ==========

def get_spot_usdt_top_symbols(limit=TOP_LIMIT):
    """
    OKX SPOT tickers → USDT pariteleri içinden en yüksek 24h notional hacme göre ilk N'i alır.
    instId formatı: BTC-USDT, HBAR-USDT vs.
    """
    data = okx_jget("/api/v5/market/tickers", {"instType": "SPOT"})
    if not data:
        return []

    rows = []
    for d in data:
        inst_id = d.get("instId", "")
        if not inst_id.endswith("-USDT"):
            continue
        volCcy24h = d.get("volCcy24h")  # quote currency volume
        try:
            vol_quote = float(volCcy24h)
        except Exception:
            vol_quote = 0.0
        rows.append((inst_id, vol_quote))

    rows.sort(key=lambda x: x[1], reverse=True)
    symbols = [r[0] for r in rows[:limit]]
    return symbols


def get_candles(inst_id, bar="4H", limit=200):
    data = okx_jget("/api/v5/market/candles", {"instId": inst_id, "bar": bar, "limit": limit})
    if not data:
        return []

    # OKX en yeni mum en üstte verir → kronolojik sıraya çevirelim
    data = list(reversed(data))

    candles = []
    for row in data:
        # [ts, o, h, l, c, vol, volCcy, ...]
        try:
            ts_ms = int(row[0])
            o = float(row[1])
            h = float(row[2])
            l = float(row[3])
            c = float(row[4])
        except Exception:
            continue
        candles.append(
            {
                "ts": ts_ms,
                "open": o,
                "high": h,
                "low": l,
                "close": c,
            }
        )
    return candles


def get_trades(inst_id, limit=TRADES_LIMIT):
    data = okx_jget("/api/v5/market/trades", {"instId": inst_id, "limit": limit})
    return data or []


def get_orderbook(inst_id, depth=ORDERBOOK_DEPTH):
    data = okx_jget("/api/v5/market/books", {"instId": inst_id, "sz": depth})
    if not data:
        return None

    book = data[0]
    bids = book.get("bids", [])
    asks = book.get("asks", [])

    def sum_notional(levels):
        total = 0.0
        for lvl in levels:
            try:
                px = float(lvl[0])
                sz = float(lvl[1])
                total += px * sz
            except Exception:
                continue
        return total

    bid_notional = sum_notional(bids)
    ask_notional = sum_notional(asks)

    best_bid = float(bids[0][0]) if bids else None
    best_ask = float(asks[0][0]) if asks else None

    return {
        "bid_notional": bid_notional,
        "ask_notional": ask_notional,
        "best_bid": best_bid,
        "best_ask": best_ask,
    }


# ========== TEKNİK HESAPLAR ==========

def ema(values, period):
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    ema_val = sum(values[:period]) / period
    for v in values[period:]:
        ema_val = v * k + ema_val * (1 - k)
    return ema_val


def analyze_trades_orderflow(trades, s_whale, m_whale, x_whale):
    """
    Spot için:
    - Net notional delta (buy_notional - sell_notional)
    - En büyük buy whale (S/M/X)
    - En büyük sell whale (S/M/X)
    - Son 20 trade momentum (buy oranı)
    """
    buy_notional = 0.0
    sell_notional = 0.0
    biggest_buy_whale = None
    biggest_sell_whale = None

    # Son 20 trade momentum (en yeni en üstte geliyor, ama sıra önemli değil)
    last_trades = trades[:20]
    buy_count = 0
    sell_count = 0

    for t in trades:
        try:
            px = float(t.get("px"))
            sz = float(t.get("sz"))
            side = t.get("side", "").lower()
        except Exception:
            continue

        notional = px * abs(sz)
        if side == "buy":
            buy_notional += notional
            # whale sınıflandırma
            tier = None
            if notional >= x_whale:
                tier = "X"
            elif notional >= m_whale:
                tier = "M"
            elif notional >= s_whale:
                tier = "S"

            if tier:
                if (biggest_buy_whale is None) or (notional > biggest_buy_whale["usd"]):
                    biggest_buy_whale = {
                        "px": px,
                        "sz": sz,
                        "usd": notional,
                        "side": side,
                        "ts": t.get("ts"),
                        "tier": tier,
                    }

        elif side == "sell":
            sell_notional += notional
            tier = None
            if notional >= x_whale:
                tier = "X"
            elif notional >= m_whale:
                tier = "M"
            elif notional >= s_whale:
                tier = "S"

            if tier:
                if (biggest_sell_whale is None) or (notional > biggest_sell_whale["usd"]):
                    biggest_sell_whale = {
                        "px": px,
                        "sz": sz,
                        "usd": notional,
                        "side": side,
                        "ts": t.get("ts"),
                        "tier": tier,
                    }

    # Son 20 trade momentum
    for t in last_trades:
        side = (t.get("side") or "").lower()
        if side == "buy":
            buy_count += 1
        elif side == "sell":
            sell_count += 1
    total_last = buy_count + sell_count
    if total_last > 0:
        buy_ratio = buy_count / total_last
        sell_ratio = sell_count / total_last
    else:
        buy_ratio = 0.5
        sell_ratio = 0.5

    net_delta = buy_notional - sell_notional

    return {
        "buy_notional": buy_notional,
        "sell_notional": sell_notional,
        "net_delta": net_delta,
        "buy_whale": biggest_buy_whale,
        "sell_whale": biggest_sell_whale,
        "has_buy_whale": biggest_buy_whale is not None,
        "has_sell_whale": biggest_sell_whale is not None,
        "buy_ratio": buy_ratio,
        "sell_ratio": sell_ratio,
    }


# ---- Market Structure Break (MSB) ----

def detect_bullish_msb(candles, lookback):
    """
    Basit bullish MSB:
    - Son lookback içindeki en yüksek kapanış alınır
    - Son mum bu seviyenin %0.1 üstünde kapanmışsa bullish break
    """
    if len(candles) < lookback + 2:
        return False, None

    closes = [c["close"] for c in candles[-(lookback + 1):-1]]
    level = max(closes)
    last_close = candles[-1]["close"]

    if last_close > level * 1.001:
        return True, level
    return False, level


def detect_bearish_msb(candles, lookback):
    """
    Basit bearish MSB:
    - Son lookback içindeki en düşük kapanış alınır
    - Son mum bu seviyenin %0.1 altına kırdıysa bearish break
    """
    if len(candles) < lookback + 2:
        return False, None

    closes = [c["close"] for c in candles[-(lookback + 1):-1]]
    level = min(closes)
    last_close = candles[-1]["close"]

    if last_close < level * 0.999:
        return True, level
    return False, level


# ---- FVG (Fair Value Gap) Tespiti ----

def find_recent_fvg(candles, lookback):
    """
    Basitleştirilmiş FVG:
    - i-2 ve i mumları arasında gap varsa:
      Bullish FVG: high(i-2) < low(i) → gap aşağıda, destek bölgesi
      Bearish FVG: low(i-2) > high(i) → gap yukarıda, direnç bölgesi
    Son lookback içinde en son görülen FVG'yi döndürür.
    """
    n = len(candles)
    if n < 3:
        return None

    start = max(2, n - lookback)
    last_fvg = None

    for i in range(start, n):
        c1 = candles[i - 2]
        c3 = candles[i]

        # Bullish FVG (gap aşağıda)
        if c1["high"] < c3["low"]:
            zone_low = c1["high"]
            zone_high = c3["low"]
            last_fvg = {
                "type": "bullish",
                "low": zone_low,
                "high": zone_high,
            }

        # Bearish FVG (gap yukarıda)
        if c1["low"] > c3["high"]:
            zone_low = c3["high"]
            zone_high = c1["low"]
            last_fvg = {
                "type": "bearish",
                "low": zone_low,
                "high": zone_high,
            }

    return last_fvg


def check_fvg_rejection(candles, fvg):
    """
    Son mum için FVG rejection kontrolü:
    - Bullish: son mum fitili FVG içine girip, FVG üstünden kapanmışsa
    - Bearish: son mum fitili FVG içine girip, FVG altından kapanmışsa
    """
    if not fvg or len(candles) < 1:
        return False

    last = candles[-1]
    low = last["low"]
    high = last["high"]
    close = last["close"]
    op = last["open"]

    z_low = fvg["low"]
    z_high = fvg["high"]

    # mum FVG bölgesine değmiş mi?
    touched = not (high < z_low or low > z_high)
    if not touched:
        return False

    # Fitil/gövde oranı çok bozuksa (fake spike) filtrele
    body = abs(close - op)
    wick = (high - low)
    if body > 0 and wick / body > 4.0:
        return False

    # bullish FVG rejection
    if fvg["type"] == "bullish":
        if close > op and close > (z_low * (1 + ZONE_BUFFER / 2)):
            return True
        return False

    # bearish FVG rejection
    if fvg["type"] == "bearish":
        if close < op and close < (z_high * (1 - ZONE_BUFFER / 2)):
            return True
        return False

    return False


# ========== STOP / TP HESAPLAMA ==========

def compute_levels(side, last_close, candles, msb_level, fvg):
    """
    Basit stop / TP hesaplama.
    FVG ve MSB bilgilerini kullanır; yoksa fallback olarak % stop/tp kullanır.
    """
    stop = None
    tp1 = None
    tp2 = None
    tp3 = None

    if side == "LONG":
        # STOP
        if fvg and fvg.get("type") == "bullish":
            stop = fvg["low"] * 0.995
        elif msb_level:
            stop = msb_level * 0.995
        else:
            stop = last_close * 0.982  # ~%1.8

        # TP'ler
        tp1 = last_close * 1.012  # %1.2
        tp2 = last_close * 1.024  # %2.4
        if msb_level and msb_level > last_close:
            tp3 = msb_level
        else:
            tp3 = last_close * 1.036  # ~%3.6

    else:  # SHORT
        if fvg and fvg.get("type") == "bearish":
            stop = fvg["high"] * 1.005
        elif msb_level:
            stop = msb_level * 1.005
        else:
            stop = last_close * 1.018  # ~%1.8

        tp1 = last_close * 0.988  # -%1.2
        tp2 = last_close * 0.976  # -%2.4
        if msb_level and msb_level < last_close:
            tp3 = msb_level
        else:
            tp3 = last_close * 0.964  # ~%3.6

    return stop, tp1, tp2, tp3


# ========== MARKET BIAS (BTC / ETH) ==========

def get_trend_summary(inst_id):
    candles_4h = get_candles(inst_id, bar="4H", limit=CANDLE_LIMIT_4H)
    candles_1h = get_candles(inst_id, bar="1H", limit=CANDLE_LIMIT_1H)

    if len(candles_4h) < 50 or len(candles_1h) < 50:
        return None

    closes_4h = [c["close"] for c in candles_4h]
    closes_1h = [c["close"] for c in candles_1h]
    last_4h = closes_4h[-1]
    last_1h = closes_1h[-1]

    ema200_4h = ema(closes_4h, 200) if len(closes_4h) >= 200 else None
    ema_fast_4h = ema(closes_4h, 12)
    ema_slow_4h = ema(closes_4h, 26)
    macd_4h = None
    if ema_fast_4h is not None and ema_slow_4h is not None:
        macd_4h = ema_fast_4h - ema_slow_4h

    ema_fast_1h = ema(closes_1h, 12)
    ema_slow_1h = ema(closes_1h, 26)

    # 4H Trend
    if ema200_4h is None:
        trend_4h = "Veri az"
    else:
        if last_4h > ema200_4h * 1.01:
            trend_4h = "Yukarı"
        elif last_4h < ema200_4h * 0.99:
            trend_4h = "Aşağı"
        else:
            trend_4h = "Yatay"

    # 4H Momentum
    if macd_4h is None:
        mom_4h = "Bilinmiyor"
    else:
        if macd_4h > 0:
            mom_4h = "Pozitif"
        elif macd_4h < 0:
            mom_4h = "Negatif"
        else:
            mom_4h = "Düz"

    # 1H kısa vade eğilim
    if ema_fast_1h is None or ema_slow_1h is None:
        trend_1h = "Bilinmiyor"
    else:
        if ema_fast_1h > ema_slow_1h * 1.002:
            trend_1h = "Yukarı"
        elif ema_fast_1h < ema_slow_1h * 0.998:
            trend_1h = "Aşağı"
        else:
            trend_1h = "Yatay"

    # Orderflow & whale bilgisi için son trades
    trades = get_trades(inst_id)
    base = inst_id.split("-")[0]
    _, seg_label, s_whale, m_whale, x_whale = get_mcap_segment(base)
    of = analyze_trades_orderflow(trades, s_whale, m_whale, x_whale) if trades else None

    whale_txt = "Anlamlı BUY whale yok"
    delta_txt = "Net delta: Veri yok"
    if of:
        delta_txt = f"Net delta: {of['net_delta']:.0f} USDT"
        bw = of["buy_whale"]
        if bw:
            whale_txt = f"Whale: {bw['tier']}-BUY ~${bw['usd']:,.0f}"
        else:
            whale_txt = "Anlamlı BUY whale yok"

    return {
        "inst_id": inst_id,
        "last": last_4h,
        "trend_4h": trend_4h,
        "mom_4h": mom_4h,
        "trend_1h": trend_1h,
        "delta_txt": delta_txt,
        "whale_txt": whale_txt,
        "segment_label": seg_label,
    }


def get_market_bias(btc_info, eth_info):
    """
    BTC 4H + 1H bilgisine göre global LONG/SHORT bias çıkar.
    """
    if not btc_info:
        return "neutral"

    bias = "neutral"

    # Basit kural:
    # 4H yukarı + 1H yukarı → bull
    # 4H aşağı + 1H aşağı → bear
    t4 = btc_info["trend_4h"]
    t1 = btc_info["trend_1h"]

    if t4 == "Yukarı" and t1 == "Yukarı":
        bias = "bull"
    elif t4 == "Aşağı" and t1 == "Aşağı":
        bias = "bear"
    else:
        bias = "neutral"

    return bias


# ========== 4H KESİN SİNYAL ANALİZİ ==========

def analyze_symbol_4h(inst_id, market_bias):
    """
    Tek coin için 4H KESİN sinyal analizi.
    FVG + MSB yapısı + orderflow + whale + orderbook.
    Sadece son 4H mumunun yaşı 90 dakikadan küçükse sinyal üretir (kapanış sonrası).
    """
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    candles = get_candles(inst_id, bar="4H", limit=CANDLE_LIMIT_4H)
    if len(candles) < STRUCT_LOOKBACK_4H + 3:
        return []

    last_candle = candles[-1]
    age_ms = now_ms - last_candle["ts"]
    if age_ms > MAX_4H_AGE_MS:
        # Son 4H mumu 1.5 saatten daha eski → yeni kapanış değil → sinyal üretme
        return []

    base = inst_id.split("-")[0]
    _, seg_label, s_whale, m_whale, x_whale = get_mcap_segment(base)

    trades = get_trades(inst_id)
    if not trades:
        return []

    of = analyze_trades_orderflow(trades, s_whale, m_whale, x_whale)
    book = get_orderbook(inst_id)
    if not book:
        return []

    bid_n = book["bid_notional"]
    ask_n = book["ask_notional"]

    # Yapı: MSB + FVG
    bullish_msb, bull_level = detect_bullish_msb(candles, STRUCT_LOOKBACK_4H)
    bearish_msb, bear_level = detect_bearish_msb(candles, STRUCT_LOOKBACK_4H)
    fvg = find_recent_fvg(candles, STRUCT_LOOKBACK_4H)

    bullish_fvg_reject = False
    bearish_fvg_reject = False
    if fvg:
        rej = check_fvg_rejection(candles, fvg)
        if rej and fvg["type"] == "bullish":
            bullish_fvg_reject = True
        if rej and fvg["type"] == "bearish":
            bearish_fvg_reject = True

    structure_long = bullish_msb or bullish_fvg_reject
    structure_short = bearish_msb or bearish_fvg_reject

    signals = []

    # ---------- LONG 4H ----------
    if structure_long and market_bias != "bear":
        cond_struct = True
        cond_delta = of["net_delta"] >= NET_DELTA_MIN_POS
        cond_ob = bid_n > ask_n * ORDERBOOK_IMB_RATIO
        cond_whale = of["has_buy_whale"]
        cond_flow = of["buy_ratio"] > 0.55  # son 20 trade daha çok buy

        conds = [cond_struct, cond_delta, cond_ob, cond_whale, cond_flow]
        true_count = sum(conds)

        if true_count >= MIN_CONDITIONS_STRICT:
            confidence = int((true_count / len(conds)) * 100)
            stop, tp1, tp2, tp3 = compute_levels(
                "LONG", last_candle["close"], candles, bull_level, fvg if bullish_fvg_reject else None
            )
            signal = {
                "inst_id": inst_id,
                "side": "LONG",
                "last_close": last_candle["close"],
                "orderflow": of,
                "orderbook": book,
                "confidence": confidence,
                "structure": {
                    "bull_msb": bullish_msb,
                    "bull_level": bull_level,
                    "bull_fvg_reject": bullish_fvg_reject,
                },
                "segment_label": seg_label,
                "stop": stop,
                "tp1": tp1,
                "tp2": tp2,
                "tp3": tp3,
            }
            signals.append(signal)

    # ---------- SHORT 4H ----------
    if structure_short and market_bias != "bull":
        cond_struct_s = True
        cond_delta_s = of["net_delta"] <= NET_DELTA_MIN_NEG
        cond_ob_s = ask_n > bid_n * ORDERBOOK_IMB_RATIO
        cond_whale_s = of["has_sell_whale"]
        cond_flow_s = of["sell_ratio"] > 0.55

        conds_s = [cond_struct_s, cond_delta_s, cond_ob_s, cond_whale_s, cond_flow_s]
        true_count_s = sum(conds_s)

        if true_count_s >= MIN_CONDITIONS_STRICT:
            confidence_s = int((true_count_s / len(conds_s)) * 100)
            stop, tp1, tp2, tp3 = compute_levels(
                "SHORT", last_candle["close"], candles, bear_level, fvg if bearish_fvg_reject else None
            )
            signal = {
                "inst_id": inst_id,
                "side": "SHORT",
                "last_close": last_candle["close"],
                "orderflow": of,
                "orderbook": book,
                "confidence": confidence_s,
                "structure": {
                    "bear_msb": bearish_msb,
                    "bear_level": bear_level,
                    "bear_fvg_reject": bearish_fvg_reject,
                },
                "segment_label": seg_label,
                "stop": stop,
                "tp1": tp1,
                "tp2": tp2,
                "tp3": tp3,
            }
            signals.append(signal)

    return signals


# ========== 1H PRE-SIGNAL ANALİZİ ==========

def analyze_symbol_1h_presignal(inst_id, market_bias):
    """
    1H ön-uyarı sinyali.
    Daha gevşek koşullar: yapı + delta veya whale veya orderflow.
    İşlem önerisi değil, "hazırlan" mesajı.
    """
    candles_1h = get_candles(inst_id, bar="1H", limit=80)
    if len(candles_1h) < 30:
        return []

    base = inst_id.split("-")[0]
    _, seg_label, s_whale, m_whale, x_whale = get_mcap_segment(base)

    trades = get_trades(inst_id)
    if not trades:
        return []

    of = analyze_trades_orderflow(trades, s_whale, m_whale, x_whale)
    book = get_orderbook(inst_id)
    if not book:
        return []

    bid_n = book["bid_notional"]
    ask_n = book["ask_notional"]
    last = candles_1h[-1]

    # 1H yapısı için daha kısa lookback
    bullish_msb_1h, bull_level_1h = detect_bullish_msb(candles_1h, lookback=15)
    bearish_msb_1h, bear_level_1h = detect_bearish_msb(candles_1h, lookback=15)
    fvg_1h = find_recent_fvg(candles_1h, lookback=15)

    bull_reject_1h = False
    bear_reject_1h = False
    if fvg_1h:
        rej = check_fvg_rejection(candles_1h, fvg_1h)
        if rej and fvg_1h["type"] == "bullish":
            bull_reject_1h = True
        if rej and fvg_1h["type"] == "bearish":
            bear_reject_1h = True

    structure_long_1h = bullish_msb_1h or bull_reject_1h
    structure_short_1h = bearish_msb_1h or bear_reject_1h

    presignals = []

    # LONG pre-signal
    if structure_long_1h and market_bias != "bear":
        cond_struct = True
        cond_delta = of["net_delta"] > 0
        cond_whale = of["has_buy_whale"]
        cond_flow = of["buy_ratio"] > 0.55
        cond_ob = bid_n > ask_n  # çok agresif olmasın, oran istemiyoruz

        conds = [cond_struct, cond_delta, cond_whale, cond_flow, cond_ob]
        true_count = sum(conds)
        if true_count >= 3:
            score = int((true_count / len(conds)) * 100)
            presignals.append(
                {
                    "inst_id": inst_id,
                    "side": "LONG",
                    "last_close": last["close"],
                    "orderflow": of,
                    "orderbook": book,
                    "score": score,
                    "segment_label": seg_label,
                    "structure": {
                        "bull_msb_1h": bullish_msb_1h,
                        "bull_fvg_1h": bull_reject_1h,
                    },
                }
            )

    # SHORT pre-signal
    if structure_short_1h and market_bias != "bull":
        cond_struct_s = True
        cond_delta_s = of["net_delta"] < 0
        cond_whale_s = of["has_sell_whale"]
        cond_flow_s = of["sell_ratio"] > 0.55
        cond_ob_s = ask_n > bid_n

        conds_s = [cond_struct_s, cond_delta_s, cond_whale_s, cond_flow_s, cond_ob_s]
        true_count_s = sum(conds_s)
        if true_count_s >= 3:
            score_s = int((true_count_s / len(conds_s)) * 100)
            presignals.append(
                {
                    "inst_id": inst_id,
                    "side": "SHORT",
                    "last_close": last["close"],
                    "orderflow": of,
                    "orderbook": book,
                    "score": score_s,
                    "segment_label": seg_label,
                    "structure": {
                        "bear_msb_1h": bearish_msb_1h,
                        "bear_fvg_1h": bear_reject_1h,
                    },
                }
            )

    return presignals


# ========== TELEGRAM MESAJI OLUŞTURMA ==========

def build_telegram_message(btc_info, eth_info, pre_signals, signals_4h):
    lines = []
    lines.append("📊 *Piyasa Durumu (BTC & ETH)*")

    if btc_info:
        lines.append(f"\n*BTC-USDT* {btc_info['segment_label']}")
        lines.append(f"- Fiyat (4H): `{btc_info['last']:.2f}`")
        lines.append(f"- 4H Trend: *{btc_info['trend_4h']}* | Momentum: *{btc_info['mom_4h']}*")
        lines.append(f"- 1H Trend: *{btc_info['trend_1h']}*")
        lines.append(f"- {btc_info['delta_txt']}")
        lines.append(f"- {btc_info['whale_txt']}")

    if eth_info:
        lines.append(f"\n*ETH-USDT* {eth_info['segment_label']}")
        lines.append(f"- Fiyat (4H): `{eth_info['last']:.2f}`")
        lines.append(f"- 4H Trend: *{eth_info['trend_4h']}* | Momentum: *{eth_info['mom_4h']}*")
        lines.append(f"- 1H Trend: *{eth_info['trend_1h']}*")
        lines.append(f"- {eth_info['delta_txt']}")
        lines.append(f"- {eth_info['whale_txt']}")

    # 1H Ön-Uyarılar
    if pre_signals:
        pre_signals_sorted = sorted(pre_signals, key=lambda x: x["score"], reverse=True)[:10]
        lines.append("\n⏳ *1H Ön-Uyarılar (Pre-Signal)*")
        for s in pre_signals_sorted:
            of = s["orderflow"]
            book = s["orderbook"]
            whale_str = "Yok"
            if s["side"] == "LONG" and of["buy_whale"]:
                bw = of["buy_whale"]
                whale_str = f"{bw['tier']}-BUY ~${bw['usd']:,.0f}"
            elif s["side"] == "SHORT" and of["sell_whale"]:
                sw = of["sell_whale"]
                whale_str = f"{sw['tier']}-SELL ~${sw['usd']:,.0f}"

            lines.append(
                f"\n*{s['inst_id']} ({s['side']})* {s['segment_label']}"
            )
            lines.append(f"- Fiyat (1H): `{s['last_close']:.4f}`")
            lines.append(f"- Net delta: `{of['net_delta']:.0f} USDT`")
            lines.append(f"- Whale: {whale_str}")
            lines.append(
                f"- Orderflow: BUY %{of['buy_ratio']*100:.0f} / SELL %{of['sell_ratio']*100:.0f}"
            )
            lines.append(f"- Skor: *%{s['score']}*")

    # 4H Kesin Sinyaller
    if signals_4h:
        sig_sorted = sorted(signals_4h, key=lambda x: x["confidence"], reverse=True)[:8]
        lines.append("\n🚀 *4H Kesin Sinyaller*")
        for s in sig_sorted:
            of = s["orderflow"]
            book = s["orderbook"]
            if s["side"] == "LONG":
                w = of["buy_whale"]
            else:
                w = of["sell_whale"]
            whale_str = "Yok"
            if w:
                whale_str = f"{w['tier']}-{w['side'].upper()} ~${w['usd']:,.0f}"

            struct_txt = []
            if s["side"] == "LONG":
                if s["structure"].get("bull_msb"):
                    struct_txt.append("Bullish MSB")
                if s["structure"].get("bull_fvg_reject"):
                    struct_txt.append("Bullish FVG retest")
            else:
                if s["structure"].get("bear_msb"):
                    struct_txt.append("Bearish MSB")
                if s["structure"].get("bear_fvg_reject"):
                    struct_txt.append("Bearish FVG retest")

            struct_str = ", ".join(struct_txt) if struct_txt else "Yapı: N/A"

            lines.append(
                f"\n*{s['inst_id']} ({s['side']})* {s['segment_label']}"
            )
            lines.append(f"- Kapanış (4H): `{s['last_close']:.4f}`")
            lines.append(f"- Yapı: {struct_str}")
            lines.append(f"- Net delta: `{of['net_delta']:.0f} USDT`")
            lines.append(
                f"- Orderbook (Bid/Ask): `{book['bid_notional']:.0f} / {book['ask_notional']:.0f}`"
            )
            lines.append(
                f"- Orderflow: BUY %{of['buy_ratio']*100:.0f} / SELL %{of['sell_ratio']*100:.0f}"
            )
            lines.append(f"- Whale: {whale_str}")
            lines.append(f"- Güven: *%{s['confidence']}*")
            lines.append(
                f"- 🎯 TP1/TP2/TP3: `{s['tp1']:.4f} / {s['tp2']:.4f} / {s['tp3']:.4f}`"
            )
            lines.append(f"- 🛑 Stop: `{s['stop']:.4f}`")

    if not pre_signals and not signals_4h:
        lines.append("\n_Bu saatte yeni pre-signal veya kesin sinyal yok._")

    lines.append(f"\n_Zaman:_ `{ts()}`")
    return "\n".join(lines)


# ========== MAIN ==========

def main():
    print(f"[{ts()}] PREMIUM PRO bot çalışıyor...")

    # 1) MCAP haritasını hazırla
    print("CoinGecko'dan market cap verileri çekiliyor...")
    build_mcap_cache()

    # 2) BTC & ETH piyasa özeti
    btc_info = get_trend_summary("BTC-USDT")
    eth_info = get_trend_summary("ETH-USDT")

    market_bias = get_market_bias(btc_info, eth_info)
    print("Market bias:", market_bias)

    # 3) Top 150 USDT spot listesi
    symbols = get_spot_usdt_top_symbols(limit=TOP_LIMIT)
    if not symbols:
        print("Top USDT listesi alınamadı.")
        return
    print(f"{len(symbols)} sembol taranıyor...")

    pre_signals = []
    signals_4h = []

    for i, inst_id in enumerate(symbols, start=1):
        print(f"[{i}/{len(symbols)}] {inst_id} analiz ediliyor...")
        try:
            # 1H pre-signal
            pres = analyze_symbol_1h_presignal(inst_id, market_bias)
            if pres:
                pre_signals.extend(pres)

            # 4H kesin sinyal
            sigs4 = analyze_symbol_4h(inst_id, market_bias)
            if sigs4:
                signals_4h.extend(sigs4)

        except Exception as e:
            print(f"  {inst_id} analiz hatası:", e)

        # Çok hızlı istek atmamak için küçük bekleme
        if i % 10 == 0:
            time.sleep(0.2)

    msg = build_telegram_message(btc_info, eth_info, pre_signals, signals_4h)
    telegram(msg)
    print("✅ Telegram'a mesaj gönderildi.")


if __name__ == "__main__":
    main()
