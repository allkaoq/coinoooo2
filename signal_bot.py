import calendar
import json
import os
import sys
import time

import requests

# ===================== 설정 (여기만 바꾸세요) =====================
# 업비트 원화마켓 코인: "코드": "표시 이름"  (코드는 업비트 앱의 'BTC/KRW' 앞부분)
COINS = {
    "BTC": "비트코인",
    "ETH": "이더리움",
    "SOL": "솔라나",
    "XRP": "리플",
    "SAND": "샌드박스",
    "POD": "돌핀",
}

# 인터벌: 업비트는 45분봉을 제공하지 않아서 15분봉 3개를 묶어 45분봉을 만듭니다.
INTERVALS = {
    "45분": {"kind": "agg", "unit": 15, "sec": 2700},
    "1시간": {"kind": "min", "unit": 60, "sec": 3600},
    "4시간": {"kind": "min", "unit": 240, "sec": 14400},
    "1일": {"kind": "day", "sec": 86400},
}

MIN_BODY_RATIO = 2.0     # 최소 몸통 비율 (지표와 동일)
RR_MULT = 2.0            # 목표 리스크/리워드 비율 (지표와 동일)
MIN_TARGET_PCT = 0.25    # 최소 목표 수익률 % (지표와 동일)
# ===============================================================

STATE_FILE = "state.json"
BASE = "https://api.upbit.com/v1"
TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")
CLOSE_BUFFER_SEC = 20    # 봉 마감 직후 데이터 반영 지연 대비


def http_get(path, params):
    for attempt in range(4):
        r = requests.get(BASE + path, params=params, timeout=15)
        if r.status_code == 429:
            time.sleep(1.5 * (attempt + 1))
            continue
        r.raise_for_status()
        time.sleep(0.15)
        return r.json()
    raise RuntimeError("업비트 요청 제한(429)")


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


def to_candle(k):
    t = calendar.timegm(time.strptime(k["candle_date_time_utc"], "%Y-%m-%dT%H:%M:%S"))
    return {"t": t, "o": float(k["opening_price"]), "h": float(k["high_price"]),
            "l": float(k["low_price"]), "c": float(k["trade_price"])}


def aggregate(candles, sec):
    buckets = {}
    for c in candles:
        b = c["t"] // sec * sec
        if b not in buckets:
            buckets[b] = {"t": b, "o": c["o"], "h": c["h"], "l": c["l"], "c": c["c"]}
        else:
            x = buckets[b]
            x["h"] = max(x["h"], c["h"])
            x["l"] = min(x["l"], c["l"])
            x["c"] = c["c"]
    return [buckets[k] for k in sorted(buckets)]


def fetch_closed(code, name):
    cfg = INTERVALS[name]
    market = f"KRW-{code}"
    if cfg["kind"] == "day":
        rows = http_get("/candles/days", {"market": market, "count": 8})
    elif cfg["kind"] == "min":
        rows = http_get(f"/candles/minutes/{cfg['unit']}", {"market": market, "count": 8})
    else:
        rows = http_get(f"/candles/minutes/{cfg['unit']}", {"market": market, "count": 40})
    candles = sorted((to_candle(k) for k in rows), key=lambda c: c["t"])
    if cfg["kind"] == "agg":
        candles = aggregate(candles, cfg["sec"])
    now = time.time()
    return [c for c in candles if c["t"] + cfg["sec"] + CLOSE_BUFFER_SEC <= now]


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
    if x >= 100:
        return f"{x:,.0f}" if x == int(x) else f"{x:,.2f}".rstrip("0").rstrip(".")
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

    try:
        valid = {m["market"] for m in http_get("/market/all", {"isDetails": "false"})}
    except Exception as e:
        print(f"마켓 목록 오류: {e}", file=sys.stderr)
        valid = None

    for code, kr in COINS.items():
        if valid is not None and f"KRW-{code}" not in valid:
            if not state.get(f"warn_{code}"):
                send(f"⚠️ 업비트 원화마켓에 '{code}'({kr})가 없습니다. signal_bot.py의 코인 코드를 확인하세요.")
                state[f"warn_{code}"] = 1
            continue
        for name in INTERVALS:
            key = f"{code}_{name}"
            try:
                closed = fetch_closed(code, name)
                if len(closed) < 3:
                    continue
                last_seen = state.get(key, closed[-2]["t"])
                for i in range(len(closed) - 3, len(closed)):
                    cur, prev = closed[i], closed[i - 1]
                    if cur["t"] <= last_seen:
                        continue
                    sig = check_signal(prev, cur)
                    if sig:
                        icon = "🚀" if sig["side"] == "롱" else "⬇️"
                        send(f"{icon} {sig['side']} 진입 | {kr}({code}) {name}\n"
                             f"진입: {fmt(sig['entry'])}원\n"
                             f"손절: {fmt(sig['stop'])}원\n"
                             f"익절: {fmt(sig['target'])}원 ({sig['reward_pct']:.2f}%)\n"
                             f"RR 1:{sig['rr']:.1f}\n"
                             f"레버리지(참고): {sig['lev']}x")
                state[key] = closed[-1]["t"]
            except Exception as e:
                print(f"{key} 오류: {e}", file=sys.stderr)

    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f)


if __name__ == "__main__":
    main()
