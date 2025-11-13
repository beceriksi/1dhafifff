import os
import time
import requests
from datetime import datetime, timezone

OKX_BASE = "https://www.okx.com"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# --- Parametreler ---
TOP_LIMIT = 150               # En çok hacimli 150 spot USDT coini
BAR = "4H"                    # 4 saatlik sistem
CANDLE_LIMIT = 200
TRADES_LIMIT = 200
ORDERBOOK_DEPTH = 20

WHALE_USDT_MIN = 300_000      # Whale alım/satım eşiği
NET_DELTA_MIN_POS = 100_000   # Long için min net alış delta (USDT)
NET_DELTA_MIN_NEG = -100_000  # Short için min net satış delta (USDT)
ORDERBOOK_IMB_RATIO = 1.4     # Bid/Ask notional dengesizliği oranı

SUPPORT_LOOKBACK = 30         # 4H destek/direnç için bakılan mum sayısı
ZONE_BUFFER = 0.002           # %0.2 marj ile bölge (destek ve dirençte)


def ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def jget(url, params=None, retries=3, timeout=10):
    for _ in range(retries):
        try:
            r = requests.get(url, params=params, timeout=timeout)
            if r.status_code == 200:
                j = r.json()
                if j.get("code") == "0" and j.get("data"):
                    return j["data"]
        except Exception:
            time.sleep(0.5)
    return None


def telegram(msg: str):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("⚠ TELEGRAM_TOKEN veya CHAT_ID yok, mesaj gönderemem.")
        print("--- Mesaj içeriği ---")
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


# ------------ OKX Yardımcıları ------------

def get_spot_usdt_top_symbols(limit=TOP_LIMIT):
    """
    OKX SPOT tickers → USDT pariteleri içinden en yüksek 24h notional hacme göre ilk 150'yi alır.
    instId formatı: BTC-USDT, HBAR-USDT vs.
    """
    url = f"{OKX_BASE}/api/v5/market/tickers"
    params = {"instType": "SPOT"}
    data = jget(url, params=params)
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


def get_candles(inst_id, bar=BAR, limit=CANDLE_LIMIT):
    url = f"{OKX_BASE}/api/v5/market/candles"
    params = {"instId": inst_id, "bar": bar, "limit": limit}
    data = jget(url, params=params)
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
    url = f"{OKX_BASE}/api/v5/market/trades"
    params = {"instId": inst_id, "limit": limit}
    data = jget(url, params=params)
    return data or []


def get_orderbook(inst_id, depth=ORDERBOOK_DEPTH):
    url = f"{OKX_BASE}/api/v5/market/books"
    params = {"instId": inst_id, "sz": depth}
    data = jget(url, params=params)
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


# ------------ Teknik Hesaplar ------------

def ema(values, period):
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    ema_val = sum(values[:period]) / period
    for v in values[period:]:
        ema_val = v * k + ema_val * (1 - k)
    return ema_val


def detect_support_zone(candles, lookback=SUPPORT_LOOKBACK, buffer_ratio=ZONE_BUFFER):
    """
    Basit 4H destek:
    - Son lookback mum içindeki EN düşük seviye → destek
    - Son mum bu bölgeyi test edip üstünde kapanmışsa True
    """
    if len(candles) < lookback + 2:
        return False, None, None

    past = candles[-(lookback + 1):-1]  # son mum hariç geçmiş
    last = candles[-1]

    support = min(c["low"] for c in past)
    zone_low = support
    zone_high = support * (1 + buffer_ratio)

    touched = last["low"] <= zone_high
    closed_above = last["close"] > zone_low

    is_bounce = touched and closed_above
    return is_bounce, zone_low, zone_high


def detect_resistance_zone(candles, lookback=SUPPORT_LOOKBACK, buffer_ratio=ZONE_BUFFER):
    """
    Basit 4H direnç:
    - Son lookback mum içindeki EN yüksek seviye → direnç
    - Son mum bu bölgeye fitil atıp altında kapanmışsa True
    """
    if len(candles) < lookback + 2:
        return False, None, None

    past = candles[-(lookback + 1):-1]
    last = candles[-1]

    resistance = max(c["high"] for c in past)
    zone_low = resistance * (1 - buffer_ratio)
    zone_high = resistance

    touched = last["high"] >= zone_low
    closed_below = last["close"] < zone_high

    is_reject = touched and closed_below
    return is_reject, zone_low, zone_high


def analyze_trades_orderflow(trades):
    """
    Spot için:
    - Net notional delta (buy_notional - sell_notional)
    - En büyük buy whale
    - En büyük sell whale
    """
    buy_notional = 0.0
    sell_notional = 0.0
    biggest_buy_whale = None
    biggest_sell_whale = None

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
            if notional >= WHALE_USDT_MIN:
                if (biggest_buy_whale is None) or (notional > biggest_buy_whale["usd"]):
                    biggest_buy_whale = {
                        "px": px,
                        "sz": sz,
                        "usd": notional,
                        "side": side,
                        "ts": t.get("ts"),
                    }
        elif side == "sell":
            sell_notional += notional
            if notional >= WHALE_USDT_MIN:
                if (biggest_sell_whale is None) or (notional > biggest_sell_whale["usd"]):
                    biggest_sell_whale = {
                        "px": px,
                        "sz": sz,
                        "usd": notional,
                        "side": side,
                        "ts": t.get("ts"),
                    }

    net_delta = buy_notional - sell_notional

    return {
        "buy_notional": buy_notional,
        "sell_notional": sell_notional,
        "net_delta": net_delta,
        "buy_whale": biggest_buy_whale,
        "sell_whale": biggest_sell_whale,
        "has_buy_whale": biggest_buy_whale is not None,
        "has_sell_whale": biggest_sell_whale is not None,
    }


def analyze_symbol(inst_id):
    """
    Tek coin için:
    - 4H destek bounce → LONG adayı
    - 4H direnç rejection → SHORT adayı
    - Orderflow + whale + orderbook ile filtre
    """
    candles = get_candles(inst_id)
    if len(candles) < SUPPORT_LOOKBACK + 2:
        return []  # yeterli veri yok

    last = candles[-1]
    trades = get_trades(inst_id)
    if not trades:
        return []

    of = analyze_trades_orderflow(trades)
    book = get_orderbook(inst_id)
    if not book:
        return []

    bid_n = book["bid_notional"]
    ask_n = book["ask_notional"]

    signals = []

    # ---------- LONG ---------
    is_bounce, s_low, s_high = detect_support_zone(candles)
    if is_bounce:
        net_delta_ok = of["net_delta"] >= NET_DELTA_MIN_POS
        whale_ok = of["has_buy_whale"]
        ob_ok = bid_n > ask_n * ORDERBOOK_IMB_RATIO

        conds = [net_delta_ok, whale_ok, ob_ok]
        conditions_true = sum(conds)

        if conditions_true >= 2:
            confidence = int((conditions_true / 3) * 100)
            signals.append(
                {
                    "inst_id": inst_id,
                    "side": "LONG",
                    "zone_low": s_low,
                    "zone_high": s_high,
                    "last_close": last["close"],
                    "orderflow": of,
                    "orderbook": book,
                    "confidence": confidence,
                }
            )

    # ---------- SHORT ---------
    is_reject, r_low, r_high = detect_resistance_zone(candles)
    if is_reject:
        net_delta_ok_s = of["net_delta"] <= NET_DELTA_MIN_NEG
        whale_ok_s = of["has_sell_whale"]
        ob_ok_s = ask_n > bid_n * ORDERBOOK_IMB_RATIO

        conds_s = [net_delta_ok_s, whale_ok_s, ob_ok_s]
        conditions_true_s = sum(conds_s)

        if conditions_true_s >= 2:
            confidence_s = int((conditions_true_s / 3) * 100)
            signals.append(
                {
                    "inst_id": inst_id,
                    "side": "SHORT",
                    "zone_low": r_low,
                    "zone_high": r_high,
                    "last_close": last["close"],
                    "orderflow": of,
                    "orderbook": book,
                    "confidence": confidence_s,
                }
            )

    return signals


# ------------ BTC & ETH Piyasa Özeti ------------

def get_trend_summary(inst_id):
    candles = get_candles(inst_id)
    if len(candles) < 50:
        return None

    closes = [c["close"] for c in candles]
    last = closes[-1]

    ema200 = ema(closes, 200) if len(closes) >= 200 else None
    ema_fast = ema(closes, 12)
    ema_slow = ema(closes, 26)

    macd = None
    if ema_fast is not None and ema_slow is not None:
        macd = ema_fast - ema_slow

    trades = get_trades(inst_id)
    of = analyze_trades_orderflow(trades) if trades else None

    # Trend yorumu
    if ema200 is None:
        trend_txt = "Veri az"
    else:
        if last > ema200 * 1.01:
            trend_txt = "Yukarı"
        elif last < ema200 * 0.99:
            trend_txt = "Aşağı"
            # (istersen burada short bias eklenebilir)
        else:
            trend_txt = "Yatay"

    # Momentum yorumu
    if macd is None:
        mom_txt = "Bilinmiyor"
    else:
        if macd > 0:
            mom_txt = "Pozitif"
        elif macd < 0:
            mom_txt = "Negatif"
        else:
            mom_txt = "Düz"

    whale_txt = "Veri yok"
    delta_txt = "Veri yok"
    if of:
        delta_txt = f"Net delta: {of['net_delta']:.0f} USDT"
        w = of["buy_whale"]
        if w:
            whale_txt = f"Whale alım: ~${w['usd']:,.0f}"
        else:
            whale_txt = "Büyük buy whale yok"

    return {
        "inst_id": inst_id,
        "last": last,
        "trend": trend_txt,
        "momentum": mom_txt,
        "delta_txt": delta_txt,
        "whale_txt": whale_txt,
    }


def build_telegram_message(btc_info, eth_info, signals):
    lines = []
    lines.append(f"*📊 Piyasa Trendi (4H – OKX)*")

    if btc_info:
        lines.append(f"\n*BTC-USDT*")
        lines.append(f"- Fiyat: `{btc_info['last']:.2f}`")
        lines.append(f"- Trend: *{btc_info['trend']}*")
        lines.append(f"- Momentum: *{btc_info['momentum']}*")
        lines.append(f"- {btc_info['delta_txt']}")
        lines.append(f"- {btc_info['whale_txt']}")

    if eth_info:
        lines.append(f"\n*ETH-USDT*")
        lines.append(f"- Fiyat: `{eth_info['last']:.2f}`")
        lines.append(f"- Trend: *{eth_info['trend']}*")
        lines.append(f"- Momentum: *{eth_info['momentum']}*")
        lines.append(f"- {eth_info['delta_txt']}")
        lines.append(f"- {eth_info['whale_txt']}")

    lines.append(f"\n*🚀 4H Giriş Sinyalleri (Top {TOP_LIMIT} USDT Spot)*")

    if not signals:
        lines.append("_Bu taramada sinyal yok._")
    else:
        for s in signals:
            of = s["orderflow"]
            book = s["orderbook"]

            if s["side"] == "LONG":
                w = of["buy_whale"]
                whale_str = "Yok"
                if w:
                    whale_str = f"BUY ~${w['usd']:,.0f} @ {w['px']:.4f}"
                zone_label = "4H Destek bölgesi"
            else:
                w = of["sell_whale"]
                whale_str = "Yok"
                if w:
                    whale_str = f"SELL ~${w['usd']:,.0f} @ {w['px']:.4f}"
                zone_label = "4H Direnç bölgesi"

            lines.append(f"\n*{s['inst_id']} ({s['side']})*")
            lines.append(f"- {zone_label}: `{s['zone_low']:.4f} – {s['zone_high']:.4f}`")
            lines.append(f"- Kapanış: `{s['last_close']:.4f}`")
            lines.append(f"- Net delta: `{of['net_delta']:.0f} USDT`")
            lines.append(f"- Whale: {whale_str}")
            lines.append(
                f"- Orderbook (Bid/Ask notional): `{book['bid_notional']:.0f} / {book['ask_notional']:.0f}`"
            )
            lines.append(f"- Güven: *%{s['confidence']}*")

    lines.append(f"\n_Zaman:_ `{ts()}`")
    return "\n".join(lines)


def main():
    print(f"[{ts()}] Bot çalışıyor...")

    # BTC & ETH piyasa özeti
    btc_info = get_trend_summary("BTC-USDT")
    eth_info = get_trend_summary("ETH-USDT")

    # Top 150 USDT spot listesi
    symbols = get_spot_usdt_top_symbols(limit=TOP_LIMIT)
    if not symbols:
        print("Top USDT listesi alınamadı.")
        return

    print(f"{len(symbols)} sembol taranıyor...")

    all_signals = []
    for i, inst_id in enumerate(symbols, start=1):
        print(f"[{i}/{len(symbols)}] {inst_id} analiz ediliyor...")
        try:
            sigs = analyze_symbol(inst_id)
            if sigs:
                for s in sigs:
                    print(f"  → Sinyal bulundu: {inst_id} ({s['side']})")
                all_signals.extend(sigs)
        except Exception as e:
            print(f"  {inst_id} analiz hatası:", e)
        time.sleep(0.15)  # çok hızlı istek atıp ban yememek için küçük bekleme

    if not all_signals:
        print("Bu turda sinyal yok. Telegram'a mesaj gönderilmeyecek.")
        return

    msg = build_telegram_message(btc_info, eth_info, all_signals)
    telegram(msg)
    print("✅ Telegram'a sinyal mesajı gönderildi.")


if __name__ == "__main__":
    main()
