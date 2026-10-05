import calendar
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

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
    "5분": {"kind": "min", "unit": 5, "sec": 300},
    "10분": {"kind": "min", "unit": 10, "sec": 600},
    "45분": {"kind": "agg", "unit": 15, "sec": 2700},
    "1시간": {"kind": "min", "unit": 60, "sec": 3600},
    "4시간": {"kind": "min", "unit": 240, "sec": 14400},
    "1일": {"kind": "day", "sec": 86400},
}

MIN_BODY_RATIO = 2.0     # 최소 몸통 비율 (지표와 동일)
RR_MULT = 2.0            # 목표 리스크/리워드 비율 (지표와 동일)
MIN_TARGET_PCT = 0.25    # 최소 목표 수익률 % (지표와 동일)
REQUIRE_FVG = False      # True면 FVG가 있어야만 알림 (지표의 'FVG 필수 조건')
TREND_LEN = 10           # 트렌드라인 피봇 길이 (지표와 동일)
PARALLEL_TOL = 20.0      # 채널 평행 허용 오차 % (지표와 동일)
KEY_LOOKBACK = 20        # 주요 레벨 룩백 (지표와 동일)
WICK_RATIO_MIN = 0.5     # 최소 꼬리 비율 (지표와 동일)
DOUBLE_TOL = 0.3         # 더블탑/바텀 허용 오차 % (지표와 동일)
VOL_LOOKBACK = 20        # 거래량 평균 기간 (지표와 동일)
VOL_SPIKE_MULT = 1.5     # 거래량 급증 기준 (지표와 동일)
SCORE_MIN = 3            # 롱/숏 점수가 이 값 이상이면 알림 (최대 8점)
SCORE_ONLY_ON_CROSS = True   # True: 점수가 3점 미만 -> 3점 이상으로 '올라설 때'만 알림 / False: 3점 이상인 봉마다 알림
FLOW_LEN = 20            # 체결 흐름 계산 기간 (지표와 동일)
LOOP_MINUTES = float(os.environ.get("LOOP_MINUTES", "0") or 0)   # 0이면 1회 실행, 양수면 그 시간 동안 1분마다 반복
POLL_SEC = 30            # 반복 확인 간격(초)
POLL_OFFSET_SEC = 10     # 매 30초 주기의 10초 지점(:10, :40)에 확인 (봉 마감 직후 빠르게 감지)
# ===============================================================

HEARTBEAT_HOUR_KST = 9   # 매일 이 시각(한국시간) 이후 첫 실행 때 "작동 중" 알림 1회. 끄려면 None
KST = timezone(timedelta(hours=9))
STATE_FILE = "state.json"
BASE = "https://api.upbit.com/v1"
TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")
CLOSE_BUFFER_SEC = 8     # 봉 마감 직후 데이터 반영 지연 대비(초)


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
            "l": float(k["low_price"]), "c": float(k["trade_price"]),
            "v": float(k.get("candle_acc_trade_volume", 0))}


def aggregate(candles, sec):
    buckets = {}
    for c in candles:
        b = c["t"] // sec * sec
        if b not in buckets:
            buckets[b] = {"t": b, "o": c["o"], "h": c["h"], "l": c["l"], "c": c["c"], "v": c["v"]}
        else:
            x = buckets[b]
            x["h"] = max(x["h"], c["h"])
            x["l"] = min(x["l"], c["l"])
            x["c"] = c["c"]
            x["v"] += c["v"]
    return [buckets[k] for k in sorted(buckets)]


def fetch_closed(code, name):
    cfg = INTERVALS[name]
    market = f"KRW-{code}"
    if cfg["kind"] == "day":
        rows = http_get("/candles/days", {"market": market, "count": 200})
    elif cfg["kind"] == "min":
        rows = http_get(f"/candles/minutes/{cfg['unit']}", {"market": market, "count": 200})
    else:
        rows = http_get(f"/candles/minutes/{cfg['unit']}", {"market": market, "count": 200})
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


def ema_series(values, n):
    alpha = 2.0 / (n + 1)
    out, e = [], None
    for v in values:
        e = v if e is None else alpha * v + (1 - alpha) * e
        out.append(e)
    return out


def pivot_list(vals, left, right, is_high):
    res = []
    for p in range(left, len(vals) - right):
        v = vals[p]
        l, r = vals[p - left:p], vals[p + 1:p + right + 1]
        if is_high:
            ok = all(v > x for x in l) and all(v >= x for x in r)
        else:
            ok = all(v < x for x in l) and all(v <= x for x in r)
        if ok:
            res.append(p)
    return res


def analyze(cs):
    """지표의 FVG / EMA / 구조 / Fake out / Trap / 채널 / 거래량 판단을 봉 전체에 대해 계산"""
    n = len(cs)
    o = [c["o"] for c in cs]; h = [c["h"] for c in cs]
    l = [c["l"] for c in cs]; cl = [c["c"] for c in cs]; v = [c.get("v", 0.0) for c in cs]
    an = {"cs": cs, "ema": ema_series(cl, 50),
          "p3h": pivot_list(h, 3, 3, True), "p3l": pivot_list(l, 3, 3, False),
          "p10h": pivot_list(h, TREND_LEN, TREND_LEN, True), "p10l": pivot_list(l, TREND_LEN, TREND_LEN, False),
          "fo_up": [], "fo_dn": [], "trap_up": [], "trap_dn": []}
    for j in range(n):
        if j >= KEY_LOOKBACK + 1:
            kr = max(h[j - KEY_LOOKBACK:j]); ks = min(l[j - KEY_LOOKBACK:j])
            rng = h[j] - l[j]
            if rng > 0:
                upw = h[j] - max(cl[j], o[j]); dnw = min(cl[j], o[j]) - l[j]
                if h[j] > kr and cl[j] < kr and upw / rng >= WICK_RATIO_MIN:
                    if not an["fo_up"] or j - an["fo_up"][-1] > 5:
                        an["fo_up"].append(j)
                if l[j] < ks and cl[j] > ks and dnw / rng >= WICK_RATIO_MIN:
                    if not an["fo_dn"] or j - an["fo_dn"][-1] > 5:
                        an["fo_dn"].append(j)
        ph = [p for p in an["p3h"] if p + 3 <= j]
        pl = [p for p in an["p3l"] if p + 3 <= j]
        if len(ph) >= 2:
            ph1, ph2 = h[ph[-2]], h[ph[-1]]
            if abs(ph2 - ph1) <= ph1 * DOUBLE_TOL / 100 and cl[j] < ph1 * (1 - DOUBLE_TOL / 100):
                if not an["trap_up"] or j - an["trap_up"][-1] > 10:
                    an["trap_up"].append(j)
        if len(pl) >= 2:
            pl1, pl2 = l[pl[-2]], l[pl[-1]]
            if abs(pl2 - pl1) <= pl1 * DOUBLE_TOL / 100 and cl[j] > pl1 * (1 + DOUBLE_TOL / 100):
                if not an["trap_dn"] or j - an["trap_dn"][-1] > 10:
                    an["trap_dn"].append(j)
    an["o"], an["h"], an["l"], an["c"], an["v"] = o, h, l, cl, v
    return an


def recent(events, i, within):
    ev = [e for e in events if e <= i]
    return bool(ev) and i - ev[-1] <= within


def channel_at(an, i):
    ph = [p for p in an["p10h"] if p + TREND_LEN <= i]
    pl = [p for p in an["p10l"] if p + TREND_LEN <= i]
    if len(ph) < 2 or len(pl) < 2:
        return "없음", None, None
    h, l, close = an["h"], an["l"], an["c"][i]
    up = (h[ph[-1]] - h[ph[-2]]) / (ph[-1] - ph[-2])
    lo = (l[pl[-1]] - l[pl[-2]]) / (pl[-1] - pl[-2])
    flat = close * 0.0004
    up_flat, lo_flat = abs(up) < flat, abs(lo) < flat
    ref = max(abs(up), abs(lo), flat)
    parallel = abs(up - lo) <= ref * PARALLEL_TOL / 100
    ctype = "없음"
    if parallel:
        if up_flat and lo_flat:
            ctype = "수평 채널"
        elif up > 0 and lo > 0:
            ctype = "상승 채널"
        elif up < 0 and lo < 0:
            ctype = "하락 채널"
    elif up > lo and lo >= 0:
        ctype = "확장 채널"
    elif up < lo and up <= 0:
        ctype = "확장 채널"
    upper_now = h[ph[-1]] + up * (i - ph[-1])
    lower_now = l[pl[-1]] + lo * (i - pl[-1])
    return ctype, upper_now, lower_now


def build_reasons(side, an, i):
    o, h, l, c, v = an["o"], an["h"], an["l"], an["c"], an["v"]
    close = c[i]
    long_side = side == "롱"
    out = []
    out.append("• 상승 장악형 오더블록" if long_side else "• 하락 장악형 오더블록")
    if i >= 2:
        if long_side and l[i] > h[i - 2] and c[i] > c[i - 2]:
            out.append("• 상승 FVG 발생")
        if not long_side and h[i] < l[i - 2] and c[i] < c[i - 2]:
            out.append("• 하락 FVG 발생")
    if long_side and close > an["ema"][i]:
        out.append("• 50 EMA 상단")
    if not long_side and close < an["ema"][i]:
        out.append("• 50 EMA 하단")
    ph10 = [p for p in an["p10h"] if p + TREND_LEN <= i]
    pl10 = [p for p in an["p10l"] if p + TREND_LEN <= i]
    if long_side and len(pl10) >= 2 and close > l[pl10[-1]]:
        out.append("• 상승 트렌드라인 지지")
    if not long_side and len(ph10) >= 2 and close < h[ph10[-1]]:
        out.append("• 하락 트렌드라인 저항")
    ph3 = [p for p in an["p3h"] if p + 3 <= i]
    pl3 = [p for p in an["p3l"] if p + 3 <= i]
    if long_side and len(pl3) >= 2 and l[pl3[-1]] > l[pl3[-2]]:
        out.append("• 고점/저점 상승 구조")
    if not long_side and len(ph3) >= 2 and h[ph3[-1]] < h[ph3[-2]]:
        out.append("• 고점/저점 하락 구조")
    if long_side and recent(an["fo_dn"], i, 5):
        out.append("• 하단 Fake out 이후 복귀")
    if not long_side and recent(an["fo_up"], i, 5):
        out.append("• 상단 Fake out 이후 복귀")
    if long_side and recent(an["trap_dn"], i, 5):
        out.append("• 더블바텀 Trap 패턴")
    if not long_side and recent(an["trap_up"], i, 5):
        out.append("• 더블탑 Trap 패턴")
    ctype, upper_now, lower_now = channel_at(an, i)
    if long_side and ctype in ("상승 채널", "수평 채널") and lower_now is not None and close <= lower_now * 1.01:
        out.append("• 채널 하단 지지 구간")
    if not long_side and ctype in ("하락 채널", "수평 채널") and upper_now is not None and close >= upper_now * 0.99:
        out.append("• 채널 상단 저항 구간")
    rel = None
    if i >= VOL_LOOKBACK - 1:
        avg = sum(v[i - VOL_LOOKBACK + 1:i + 1]) / VOL_LOOKBACK
        if avg > 0:
            rel = v[i] / avg * 100
            if v[i] >= avg * VOL_SPIKE_MULT:
                out.append("• 거래량 급증 확인")
    return out, rel


def fvg_at(an, i, long_side):
    if i < 2:
        return False
    l, h, c = an["l"], an["h"], an["c"]
    return (l[i] > h[i - 2] and c[i] > c[i - 2]) if long_side else (h[i] < l[i - 2] and c[i] < c[i - 2])


def fmt_vol(x):
    return f"{x:,.0f}" if x >= 1000 else f"{x:,.2f}"


def fmt(x):
    if x >= 100:
        return f"{x:,.0f}" if x == int(x) else f"{x:,.2f}".rstrip("0").rstrip(".")
    return f"{x:,.4f}".rstrip("0").rstrip(".")


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def ob_kind(an, j):
    """j번째 봉에서 오더블록 발생 여부: '상승' / '하락' / None"""
    if j < 1:
        return None
    o, c = an["o"], an["c"]
    body, pbody = abs(c[j] - o[j]), abs(c[j - 1] - o[j - 1])
    if body < pbody * MIN_BODY_RATIO:
        return None
    if c[j - 1] > o[j - 1] and c[j] < o[j] and c[j] < o[j - 1] and o[j] > c[j - 1]:
        return "하락"
    if c[j - 1] < o[j - 1] and c[j] > o[j] and c[j] > o[j - 1] and o[j] < c[j - 1]:
        return "상승"
    return None


def fvg_kind(an, j):
    if j < 2:
        return None
    l, h, c = an["l"], an["h"], an["c"]
    if l[j] > h[j - 2] and c[j] > c[j - 2]:
        return "상승 갭"
    if h[j] < l[j - 2] and c[j] < c[j - 2]:
        return "하락 갭"
    return None


def score_at(an, i):
    """지표의 롱/숏 종합 점수(각 항목 1점, 최대 8점)를 i번째 봉 기준으로 계산"""
    if i < max(FLOW_LEN, VOL_LOOKBACK, 15):
        return None
    o, h, l, c, v = an["o"], an["h"], an["l"], an["c"], an["v"]
    buy_sum = sell_sum = 0.0
    for j in range(i - FLOW_LEN + 1, i + 1):
        rng = h[j] - l[j]
        br = (c[j] - l[j]) / rng if rng > 0 else 0.5
        buy_sum += v[j] * br
        sell_sum += v[j] * (1 - br)
    strength = buy_sum / sell_sum * 100 if sell_sum > 0 else 100.0
    vol_sum = sum(v[i - FLOW_LEN + 1:i + 1])
    delta_ratio = (buy_sum - sell_sum) / vol_sum if vol_sum > 0 else 0.0
    avg_v = sum(v[i - VOL_LOOKBACK + 1:i + 1]) / VOL_LOOKBACK
    rel_vol = v[i] / avg_v * 100 if avg_v > 0 else None
    tv = [v[j] * (h[j] + l[j] + c[j]) / 3 for j in range(i - VOL_LOOKBACK + 1, i + 1)]
    avg_tv = sum(tv) / VOL_LOOKBACK
    rel_tv = tv[-1] / avg_tv * 100 if avg_tv > 0 else 100.0
    s_score = clamp((strength - 100) / 100, -1.0, 1.0)
    d_score = clamp(delta_ratio * 2, -1.0, 1.0)
    act = clamp(((rel_vol if rel_vol is not None else 100.0) + rel_tv) / 200, 0.5, 1.5)
    bias = clamp((s_score * 0.5 + d_score * 0.5) * act, -1.0, 1.0)

    ob = next((ob_kind(an, j) for j in range(i, max(0, i - 3) - 1, -1) if ob_kind(an, j)), None)
    fv = next((fvg_kind(an, j) for j in range(i, max(1, i - 10) - 1, -1) if fvg_kind(an, j)), None)
    ctype, upper_now, lower_now = channel_at(an, i)
    vol_ok = rel_vol is not None and rel_vol >= 100
    close, ema = c[i], an["ema"][i]
    long_items = [
        ("최근 3봉 내 상승 오더블록", ob == "상승"),
        ("50 EMA 상단", close > ema),
        ("체결 흐름 매수 우위", bias > 0.15),
        ("체결강도 100% 이상", strength >= 100),
        ("거래량 평균 이상", vol_ok),
        ("채널 하단 지지 구간", ctype in ("상승 채널", "수평 채널") and lower_now is not None and close <= lower_now * 1.01),
        ("최근 10봉 내 상승 FVG", fv == "상승 갭"),
        ("하단 Fake out / 더블바텀 Trap", recent(an["fo_dn"], i, 5) or recent(an["trap_dn"], i, 5)),
    ]
    short_items = [
        ("최근 3봉 내 하락 오더블록", ob == "하락"),
        ("50 EMA 하단", close < ema),
        ("체결 흐름 매도 우위", bias < -0.15),
        ("체결강도 100% 미만", strength < 100),
        ("거래량 평균 이상", vol_ok),
        ("채널 상단 저항 구간", ctype in ("하락 채널", "수평 채널") and upper_now is not None and close >= upper_now * 0.99),
        ("최근 10봉 내 하락 FVG", fv == "하락 갭"),
        ("상단 Fake out / 더블탑 Trap", recent(an["fo_up"], i, 5) or recent(an["trap_up"], i, 5)),
    ]
    return {
        "롱": {"score": sum(1 for _, ok in long_items if ok), "items": long_items,
              "warn": recent(an["fo_up"], i, 5) or recent(an["trap_up"], i, 5), "warn_text": "⚠ 상단 Fake out/Trap 최근 발생"},
        "숏": {"score": sum(1 for _, ok in short_items if ok), "items": short_items,
              "warn": recent(an["fo_dn"], i, 5) or recent(an["trap_dn"], i, 5), "warn_text": "⚠ 하단 Fake out/Trap 최근 발생"},
        "strength": strength, "bias": bias,
    }


def grade(score):
    return "강함" if score >= 6 else "보통" if score >= 4 else "약함"


def send(text):
    if not TG_TOKEN or not TG_CHAT_ID:
        sys.exit("오류: TG_TOKEN / TG_CHAT_ID 비밀값이 없습니다. "
                 "저장소 Settings → Secrets and variables → Actions 에서 이름과 값을 확인하세요.")
    r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      data={"chat_id": TG_CHAT_ID, "text": text}, timeout=15)
    if r.status_code != 200:
        raise RuntimeError(f"텔레그램 전송 실패 {r.status_code}: {r.text}")


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f)


def commit_state():
    """반복 실행 중에도 상태를 저장소에 기록 (중복 알림 방지). GitHub Actions에서만 동작"""
    if os.environ.get("COMMIT_STATE") != "1":
        return
    try:
        subprocess.run(["git", "config", "user.name", "bot"], check=False)
        subprocess.run(["git", "config", "user.email", "bot@users.noreply.github.com"], check=False)
        subprocess.run(["git", "add", STATE_FILE], check=False)
        if subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode != 0:
            subprocess.run(["git", "commit", "-m", "state"], check=False, capture_output=True)
            subprocess.run(["git", "push"], check=False, capture_output=True)
    except Exception as e:
        print(f"상태 커밋 오류: {e}", file=sys.stderr)


def dispatch_next():
    """GitHub Actions에서 다음 실행을 미리 예약 (현재 실행이 끝나면 이어서 시작되어 끊김 없이 이어짐)"""
    repo = os.environ.get("GITHUB_REPOSITORY")
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not repo or not token:
        return
    wf = os.environ.get("WORKFLOW_FILE", "signal.yml")
    ref = os.environ.get("GITHUB_REF_NAME", "main")
    try:
        r = requests.post(f"https://api.github.com/repos/{repo}/actions/workflows/{wf}/dispatches",
                          headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
                          json={"ref": ref}, timeout=15)
        print(f"다음 실행 예약: HTTP {r.status_code}")
    except Exception as e:
        print(f"다음 실행 예약 오류: {e}", file=sys.stderr)


def expected_last_closed(name, now):
    sec = INTERVALS[name]["sec"]
    return int((now - CLOSE_BUFFER_SEC) // sec) * sec - sec


def scan_once(state, checked, valid):
    """모든 코인/인터벌을 한 번 검사. 알림을 보냈으면 True"""
    sent_any = False
    now = time.time()
    for code, kr in COINS.items():
        if valid is not None and f"KRW-{code}" not in valid:
            if not state.get(f"warn_{code}"):
                send(f"⚠️ 업비트 원화마켓에 '{code}'({kr})가 없습니다. signal_bot.py의 코인 코드를 확인하세요.")
                state[f"warn_{code}"] = 1
            continue
        for name in INTERVALS:
            key = f"{code}_{name}"
            expected = expected_last_closed(name, now)
            if checked.get(key, 0) >= expected:
                continue  # 새 봉이 마감될 시간이 아직 안 됨 -> 요청 생략
            try:
                closed = fetch_closed(code, name)
                if len(closed) < 3:
                    continue
                last_seen = state.get(key, closed[-2]["t"])
                an = analyze(closed)
                for i in range(max(1, len(closed) - 6), len(closed)):
                    cur, prev = closed[i], closed[i - 1]
                    if cur["t"] <= last_seen:
                        continue
                    sig = check_signal(prev, cur)
                    if sig and REQUIRE_FVG and not fvg_at(an, i, sig["side"] == "롱"):
                        sig = None
                    sc = score_at(an, i)
                    sc_prev = score_at(an, i - 1)
                    if sig:
                        icon = "🚀" if sig["side"] == "롱" else "⬇️"
                        reasons, rel = build_reasons(sig["side"], an, i)
                        vol_text = f"\n거래량: {fmt_vol(cur['v'])}" + (f" (평균대비 {rel:.1f}%)" if rel is not None else "")
                        score_text = f"\n점수: 롱 {sc['롱']['score']}/8 · 숏 {sc['숏']['score']}/8" if sc else ""
                        bar = "━━━━━━━━━━━━━━"
                        send(f"{icon} {sig['side']} 진입 | {kr}({code}) {name}\n{bar}\n"
                             + "\n".join(reasons) + f"\n{bar}\n"
                             f"진입: {fmt(sig['entry'])}원\n"
                             f"손절: {fmt(sig['stop'])}원\n"
                             f"익절: {fmt(sig['target'])}원 (+{sig['reward_pct']:.2f}%)\n"
                             f"RR: 1:{sig['rr']:.1f}\n"
                             f"레버리지: {sig['lev']}x" + vol_text + score_text)
                        sent_any = True
                    if sc:
                        for side in ("롱", "숏"):
                            if sig and sig["side"] == side:
                                continue  # 진입 신호 메시지에 이미 점수 포함
                            cur_s = sc[side]["score"]
                            if cur_s < SCORE_MIN:
                                continue
                            if SCORE_ONLY_ON_CROSS and sc_prev and sc_prev[side]["score"] >= SCORE_MIN:
                                continue
                            lines = [f"• {t}" for t, ok in sc[side]["items"] if ok]
                            warn = f"\n{sc[side]['warn_text']}" if sc[side]["warn"] else ""
                            send(f"📊 {side} 점수 {cur_s}/8 ({grade(cur_s)}) | {kr}({code}) {name}\n"
                                 f"현재가: {fmt(cur['c'])}원\n" + "\n".join(lines) + warn +
                                 "\n(진입 신호가 아닌 점수 알림입니다)")
                            sent_any = True
                state[key] = closed[-1]["t"]
                if closed[-1]["t"] >= expected or now - (expected + INTERVALS[name]["sec"]) > 180:
                    checked[key] = expected
            except Exception as e:
                print(f"{key} 오류: {e}", file=sys.stderr)
    return sent_any


def main():
    state = load_state()
    now_kst = datetime.now(KST)
    today = now_kst.strftime("%Y-%m-%d")

    # 처음 시작할 때, 그리고 Run workflow에서 "테스트 메시지 보내기"를 체크해 실행할 때 테스트 메시지 전송
    # (전송에 실패하면 오류로 종료되고 "시작함" 기록도 남기지 않음)
    manual = os.environ.get("SEND_TEST", "").lower() == "true"
    if manual or not state.get("started"):
        send("✅ 신호 알림 봇이 시작되었습니다 (테스트 메시지)\n"
             f"코인: {', '.join(f'{kr}({code})' for code, kr in COINS.items())}\n"
             f"인터벌: {', '.join(INTERVALS)}\n"
             f"롱/숏 진입 신호, 점수 {SCORE_MIN}점 이상 알림이 이 채팅으로 옵니다.")
        state["started"] = 1
        state["hb_date"] = today
        if manual:
            return  # 테스트 실행은 메시지만 보내고 끝 (검사/상태 저장 안 함)

    try:
        valid = {m["market"] for m in http_get("/market/all", {"isDetails": "false"})}
    except Exception as e:
        print(f"마켓 목록 오류: {e}", file=sys.stderr)
        valid = None

    deadline = time.time() + LOOP_MINUTES * 60
    checked = {}
    last_commit = time.time()
    chain = os.environ.get("CHAIN_NEXT") == "1" and LOOP_MINUTES > 0
    dispatched = False
    while True:
        now_kst = datetime.now(KST)
        today = now_kst.strftime("%Y-%m-%d")
        state["last_run"] = now_kst.strftime("%m-%d %H:%M KST")
        try:
            if HEARTBEAT_HOUR_KST is not None and now_kst.hour >= HEARTBEAT_HOUR_KST and state.get("hb_date") != today:
                send(f"💓 봇 정상 작동 중 (하루 한 번 알림)\n확인 시각: {state['last_run']}")
                state["hb_date"] = today
            sent_any = scan_once(state, checked, valid)
        except SystemExit:
            raise
        except Exception as e:
            print(f"반복 오류: {e}", file=sys.stderr)
            sent_any = False
        save_state(state)
        if chain and not dispatched and time.time() > deadline - 150:
            dispatch_next()
            dispatched = True
        if sent_any or time.time() - last_commit > 600:
            commit_state()
            last_commit = time.time()
        if LOOP_MINUTES <= 0 or time.time() + POLL_SEC + 10 >= deadline:
            break
        t = time.time()
        nxt = (int((t - POLL_OFFSET_SEC) // POLL_SEC) + 1) * POLL_SEC + POLL_OFFSET_SEC   # :10, :40 초에 확인
        time.sleep(max(1, nxt - t))
    commit_state()


if __name__ == "__main__":
    main()
