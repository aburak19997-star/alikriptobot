"""
Trend + RSI/MACD Sinyal Botu — Telegram Bildirim Script'i
============================================================
Bu script, TradingView'deki Pine Script stratejimizle BİREBİR AYNI mantığı
Python'da yeniden hesaplar:
  - Trend filtresi: 4 saatlik EMA200 (fiyat üstünde mi altında mı)
  - Giriş sinyali: 15 dakikalık RSI(14) 50 çizgisini kesmesi + MACD(12,26,9)
    kesişimi, ikisi de son 5 mum içinde gerçekleşmiş olmalı
  - Coin'ler: BTC, ETH, BNB, XRP, SOL (Binance USDT-M Perpetual)

Veri kaynağı: Binance'in ücretsiz, API key gerektirmeyen genel piyasa verisi
uç noktası (fapi.binance.com/fapi/v1/klines). Hiçbir ücret veya kayıt gerekmez.

Bildirim: Telegram Bot API (ücretsiz, sınırsız).

Tekrar bildirim göndermemek için: bir sinyal sadece YENİ oluştuğunda
(önceki kontrolde yoktu, şimdi var) bildirim gönderilir. Sinyal aynı
kalırken her 15 dakikada tekrar mesaj atılmaz — bunun için state.json
dosyasında son durum saklanır.
"""

import os
import json
import time
import sys
import requests

# ============ AYARLAR (Pine Script'teki varsayılanlarla birebir aynı) ============
SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT", "SOLUSDT"]

TREND_INTERVAL = "4h"      # Trend zaman dilimi
ENTRY_INTERVAL = "15m"     # Giriş sinyali zaman dilimi
EMA_TREND_LEN = 200
RSI_LEN = 14
RSI_MIDLINE = 50.0
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
CONFIRM_WINDOW = 5         # Kaç mum içinde teyit aranacak

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
BINANCE_KLINES_URL = "https://fapi.binance.com/fapi/v1/klines"


# ============ İNDİKATÖR HESAPLAMALARI (Pine'ın ta.ema / ta.rma / ta.rsi / ta.macd ile aynı formüller) ============

def ema(values, length):
    """Pine ta.ema ile aynı: ilk değerden başlar, sonrasında üstel ağırlıklandırma."""
    if not values:
        return []
    alpha = 2.0 / (length + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(alpha * v + (1 - alpha) * out[-1])
    return out


def rma(values, length):
    """Pine ta.rma (Wilder smoothing) ile aynı: ilk `length` barın SMA'sı ile başlar."""
    n = len(values)
    if n < length:
        return [None] * n
    out = [None] * (length - 1)
    seed = sum(values[:length]) / length
    out.append(seed)
    alpha = 1.0 / length
    prev = seed
    for v in values[length:]:
        prev = (v - prev) * alpha + prev
        out.append(prev)
    return out


def rsi(closes, length):
    """Pine ta.rsi ile aynı: Wilder smoothing tabanlı klasik RSI."""
    n = len(closes)
    ups = [0.0]
    downs = [0.0]
    for i in range(1, n):
        ch = closes[i] - closes[i - 1]
        ups.append(max(ch, 0.0))
        downs.append(max(-ch, 0.0))
    rma_up = rma(ups, length)
    rma_down = rma(downs, length)
    out = [None] * n
    for i in range(n):
        if rma_up[i] is None or rma_down[i] is None:
            continue
        if rma_down[i] == 0:
            out[i] = 100.0
        else:
            rs = rma_up[i] / rma_down[i]
            out[i] = 100 - 100 / (1 + rs)
    return out


def macd(closes, fast, slow, signal):
    """Pine ta.macd ile aynı: EMA(fast) - EMA(slow), sinyal = EMA(signal) of MACD line."""
    ema_fast = ema(closes, fast)
    ema_slow = ema(closes, slow)
    macd_line = [a - b for a, b in zip(ema_fast, ema_slow)]
    signal_line = ema(macd_line, signal)
    return macd_line, signal_line


def crossed_over(a, b, idx):
    """Pine ta.crossover(a,b): a[1] < b[1] and a > b (bu bar)."""
    if idx < 1:
        return False
    a0, a1 = a[idx - 1], a[idx]
    b0, b1 = b[idx - 1], b[idx]
    if None in (a0, a1, b0, b1):
        return False
    return a0 < b0 and a1 > b1


def crossed_under(a, b, idx):
    """Pine ta.crossunder(a,b): a[1] > b[1] and a < b (bu bar)."""
    if idx < 1:
        return False
    a0, a1 = a[idx - 1], a[idx]
    b0, b1 = b[idx - 1], b[idx]
    if None in (a0, a1, b0, b1):
        return False
    return a0 > b0 and a1 < b1


def recent_cross(a, b, idx, window, direction):
    """Pine'daki `ta.barssince(cross) <= window` ile aynı: son `window+1` bar
    içinde (bu bar dahil) bir kesişim olmuş mu?"""
    for k in range(0, window + 1):
        j = idx - k
        if j < 1:
            continue
        if direction == "over" and crossed_over(a, b, j):
            return True
        if direction == "under" and crossed_under(a, b, j):
            return True
    return False


# ============ BINANCE VERİ ÇEKME ============

def get_closed_closes(symbol, interval, limit=1000):
    """Binance'ten mum verisi çeker, henüz kapanmamış (oluşmakta olan) son mumu atar."""
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    resp = requests.get(BINANCE_KLINES_URL, params=params, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    closes = [float(k[4]) for k in data]
    close_times = [int(k[6]) for k in data]
    now_ms = int(time.time() * 1000)
    if close_times and close_times[-1] > now_ms:
        closes = closes[:-1]
    return closes


# ============ TELEGRAM BİLDİRİMİ ============

def send_telegram(token, chat_id, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = requests.post(url, data={"chat_id": chat_id, "text": text}, timeout=20)
    if not resp.ok:
        print(f"[UYARI] Telegram mesajı gönderilemedi: {resp.status_code} {resp.text}")


# ============ DURUM (STATE) YÖNETİMİ — tekrar bildirim spam'ini önlemek için ============

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {s: "NONE" for s in SYMBOLS}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ============ ANA MANTIK ============

def evaluate_symbol(symbol):
    trend_closes = get_closed_closes(symbol, TREND_INTERVAL, limit=1000)
    entry_closes = get_closed_closes(symbol, ENTRY_INTERVAL, limit=1000)

    if len(trend_closes) < EMA_TREND_LEN + 5 or len(entry_closes) < 60:
        print(f"[{symbol}] Yeterli geçmiş veri yok, atlanıyor.")
        return "NONE", None

    ema_trend = ema(trend_closes, EMA_TREND_LEN)
    ema_now = ema_trend[-1]
    px = entry_closes[-1]

    rsi_series = rsi(entry_closes, RSI_LEN)
    macd_line, signal_line = macd(entry_closes, MACD_FAST, MACD_SLOW, MACD_SIGNAL)
    midline = [RSI_MIDLINE] * len(rsi_series)

    idx = len(entry_closes) - 1

    is_up = px > ema_now
    is_down = px < ema_now

    rsi_up = recent_cross(rsi_series, midline, idx, CONFIRM_WINDOW, "over")
    rsi_down = recent_cross(rsi_series, midline, idx, CONFIRM_WINDOW, "under")
    macd_up = recent_cross(macd_line, signal_line, idx, CONFIRM_WINDOW, "over")
    macd_down = recent_cross(macd_line, signal_line, idx, CONFIRM_WINDOW, "under")

    long_signal = is_up and rsi_up and macd_up
    short_signal = is_down and rsi_down and macd_down

    print(f"[{symbol}] fiyat={px:.4f} ema200(4h)={ema_now:.4f} "
          f"rsi_up={rsi_up} rsi_down={rsi_down} macd_up={macd_up} macd_down={macd_down} "
          f"-> LONG={long_signal} SHORT={short_signal}")

    if long_signal:
        return "LONG", px
    if short_signal:
        return "SHORT", px
    return "NONE", px


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[HATA] TELEGRAM_BOT_TOKEN veya TELEGRAM_CHAT_ID tanımlı değil.")
        sys.exit(1)

    state = load_state()

    for symbol in SYMBOLS:
        try:
            new_status, price = evaluate_symbol(symbol)
        except Exception as e:
            print(f"[{symbol}] HATA: {e}")
            continue

        prev_status = state.get(symbol, "NONE")

        if new_status != "NONE" and new_status != prev_status:
            emoji = "🟢" if new_status == "LONG" else "🔴"
            msg = (f"{emoji} {new_status} sinyali — {symbol}\n"
                   f"Fiyat: {price:.4f}\n"
                   f"Zaman dilimi: 15dk giriş / 4s trend\n"
                   f"Binance Demo'da işlemi manuel aç.")
            send_telegram(token, chat_id, msg)
            print(f"[{symbol}] Bildirim gönderildi: {new_status}")

        state[symbol] = new_status

    save_state(state)


if __name__ == "__main__":
    main()
