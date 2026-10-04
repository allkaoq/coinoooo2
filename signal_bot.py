import json
import os
import sys
import time

import requests

# ===================== 설정 (여기만 바꾸세요) =====================
SYMBOLS = ["BTCUSDT", "ETHUSDT"]      # 감시할 코인
INTERVALS = ["15m", "1h"]             # 감시할 인터벌 (5m, 15m, 30m, 1h, 4h ...)
MIN_BODY_RATIO = 2.0                  # 최소 몸통 비율 (지표의 '최소 몸통 비율')
RR_MULT = 2.0                         # 목표 리스크/리워드 비율 (지표와 동일)
MIN_TARGET_PCT = 0.25                 # 최소 목표 수익률 % (지표와 동일)
# ===============================================================

STATE_FILE = "state.json"
API = "https://data-api.binance.vision/api/v3/klines"
TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")


def leverage(risk_pct, rr):
    ladder = [(0.15, 20), (0.3, 15), (0.5, 12), (0.75, 10), (1.0, 8),
              (1.5, 6), (2.0, 5), (3.0, 3), (4.0, 2)]
    base = 1.0
    for limit, lev in ladder:
        if risk_pct < limit:
            base = float(lev)
            break
    if rr < 0.8:
        base = max(1.0, base * 0.5)
    elif rr < 1.0:
        base = max(2.0, base * 0.7)
    elif rr > 2.0:
        base = min(20.0, base * 1.1)
    return int(base + 0.5)


def fetch_closed(symbol, interval):
    r = requests.get(API, params={"symbol": symbol, "interval": interval, "limit": 10}, timeout=15)
    r.raise_for_status()
    now_ms = int(time.time() * 1000)
    out = []
    for k in r.json():
        if int(k[6]) < now_ms:  # 마감된 봉만
            out.append({"t": int(k[0]), "o": float(k[1]), "h": float(k[2]),
                        "l": float(k[3]), "c": float(k[4])})
    return out


def check_signal(prev, cur):
    prev_body = abs(prev["c"] - prev["o"])
    body = abs(cur["c"] - cur["o"])
    if body < prev_body * MIN_BODY_RATIO:
        return None
    bull = prev["c"] < prev["o"] and cur["c"] > cur["o"] and cur["c"] > prev["o"] and cur["o"] < prev["c"]
    bear = prev["c"] > prev["o"] and cur["c"] < cur["o"] and cur["c"] < prev["o"] and cur["o"] > prev["c"]
    if not (bull or bear):
        return None
    c = cur["c"]
    stop = prev["o"]
    risk = abs(c - stop)
    if risk <= 0:
        return None
    reward = risk * RR_MULT / 2
    target = c + reward if bull else c - reward
    reward_pct = reward / c * 100
    if reward_pct < MIN_TARGET_PCT:
        return None
    risk_pct = risk / c * 100
    return {
        "side": "롱" if bull else "숏",
        "entry": c, "stop": stop, "target": target,
        "reward_pct": reward_pct, "rr": reward / risk,
        "lev": leverage(risk_pct, reward / risk),
    }


def fmt(x):
    return f"{x:,.4f}".rstrip("0").rstrip(".")


def send(text):
    if not TG_TOKEN or not TG_CHAT_ID:
        print("텔레그램 설정(TG_TOKEN, TG_CHAT_ID)이 없습니다.\n" + text)
        return
    r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      data={"chat_id": TG_CHAT_ID, "text": text}, timeout=15)
    r.raise_for_status()


def main():
    state = {}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)

    for symbol in SYMBOLS:
        for interval in INTERVALS:
            key = f"{symbol}_{interval}"
            try:
                closed = fetch_closed(symbol, interval)
                if len(closed) < 3:
                    continue
                # 처음 실행이면 가장 최근 봉만 검사, 이후엔 못 본 봉을 모두 검사 (실행 지연 대비)
                last_seen = state.get(key, closed[-2]["t"])
                for i in range(len(closed) - 3, len(closed)):
                    cur, prev = closed[i], closed[i - 1]
                    if cur["t"] <= last_seen:
                        continue
                    sig = check_signal(prev, cur)
                    if sig:
                        icon = "🚀" if sig["side"] == "롱" else "⬇️"
                        send(f"{icon} {sig['side']} 진입 | {symbol} {interval}\n"
                             f"진입: {fmt(sig['entry'])}\n"
                             f"손절: {fmt(sig['stop'])}\n"
                             f"익절: {fmt(sig['target'])} ({sig['reward_pct']:.2f}%)\n"
                             f"RR 1:{sig['rr']:.1f}\n"
                             f"레버리지: {sig['lev']}x")
                state[key] = closed[-1]["t"]
            except Exception as e:
                print(f"{key} 오류: {e}", file=sys.stderr)

    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f)


if __name__ == "__main__":
    main()
