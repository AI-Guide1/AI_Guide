# auto_trade.py
# 23장 구글 클라우드 자동매매 봇 (시뮬레이션 전용, 실제 주문은 절대 하지 않습니다)
#
# 22장 시뮬레이터(simulator.py)의 전략과 안전장치를 그대로 옮기고,
# 크론탭이 30분마다 ‘한 번씩’ 실행하는 방식에 맞게 고친 파일입니다.
#   - 실행할 때마다 시세를 확인하고, 매매 신호가 있으면 가상 잔고로만 매수, 매도합니다.
#   - 잔고와 보유 종목은 state.json, 거래 기록은 trade_log.csv 에 저장해 다음 실행으로 이어집니다.
#   - 같은 폴더에 kakao_notify.py 와 토큰 파일이 있으면 카카오톡으로 알림을 보냅니다(23-5).
#     매매가 있으면 매매 알림, 없으면 현재 시각과 “매매 실적 없음”을 실행할 때마다 보냅니다.
#   - 총자산이 시작 자본의 90% 이하가 되면 매매를 멈추고 비상 알림을 보냅니다(23-6).
#
# 필요한 라이브러리   pip install ccxt requests
# 실행               python auto_trade.py

import csv
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import ccxt

# ─────────────────────────────────────────────
# ① 내 설정 (여기만 바꾸면 됩니다)
# ─────────────────────────────────────────────
START_CAPITAL = 10_000_000     # 시작 자본 (가상 1,000만 원)
MAX_WEIGHT = 0.25              # 종목당 최대 비중 25% (한 종목에 몰빵 방지)
STOP_LOSS = -0.07              # 손절 -7%
TAKE_PROFIT = 0.15             # 익절 +15%
EMERGENCY_RATIO = 0.90         # 총자산이 시작 자본의 90% 이하이면 비상 정지
FEE = 0.0005                   # 업비트 수수료 0.05%
SLIPPAGE = 0.001               # 슬리피지 0.1% (실제 체결가가 살짝 불리한 정도)
REPORT_EVERY_RUN = True        # 매매가 없어도 실행할 때마다 카톡으로 상태 보고 (끄려면 False)

# 22-2 추천 결과를 적용한 종목별 전략
#   timeframe  1h = 60분봉, 4h = 240분봉, 1d = 일봉
STRATEGIES = {
    "BTC/KRW": {"timeframe": "4h", "type": "bollinger", "period": 30, "k": 2.5, "sell": "middle"},
    "ETH/KRW": {"timeframe": "1d", "type": "golden_cross", "short": 5, "long": 20},
    "XRP/KRW": {"timeframe": "1h", "type": "rsi", "period": 14, "buy": 30, "sell": 70},
    "SOL/KRW": {"timeframe": "1d", "type": "bollinger", "period": 20, "k": 2.0, "sell": "lower"},
    "ADA/KRW": {"timeframe": "4h", "type": "golden_cross", "short": 5, "long": 20},
}

KST = timezone(timedelta(hours=9))   # 서버 시간대와 상관없이 한국 시간으로 표시


def now():
    return datetime.now(KST)


# ─────────────────────────────────────────────
# ② 파일 위치 (크론탭이 어디서 실행하든 이 파일이 있는 폴더 기준)
# ─────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "state.json"            # 가상 잔고, 보유 종목
TRADE_LOG = BASE_DIR / "trade_log.csv"          # 모든 거래 기록 (엑셀로 열 수 있음)
HALT_FILE = BASE_DIR / "trading_halted.json"    # 이 파일이 있으면 비상 정지 상태

# ─────────────────────────────────────────────
# ③ 카카오톡 알림 연결 (23-5를 마치기 전에는 자동으로 건너뜀)
# ─────────────────────────────────────────────
try:
    from kakao_notify import notify_trade, flush_pending, send_to_kakao
    KAKAO_READY = (BASE_DIR / "kakao_token.json").exists()
except ImportError:
    KAKAO_READY = False


def log(msg):
    print(f"[{now():%Y-%m-%d %H:%M:%S}] {msg}")


def kakao(trade_id, message):
    if not KAKAO_READY:
        return
    try:
        notify_trade(trade_id, message)
    except Exception as e:          # 알림이 실패해도 매매는 계속
        log(f"카카오톡 알림 실패 {e}")


# ─────────────────────────────────────────────
# ④ 가상 잔고 불러오기, 저장하기
# ─────────────────────────────────────────────
def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"cash": START_CAPITAL, "holdings": {}, "trade_no": 0}


def save_state(state):
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, STATE_FILE)


def write_trade_log(row):
    new = not TRADE_LOG.exists()
    with open(TRADE_LOG, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["시각", "종목", "구분", "체결가", "수량", "금액", "수익률(%)", "사유", "현금잔고"])
        w.writerow(row)


# ─────────────────────────────────────────────
# ⑤ 지표 계산 (pandas 없이 계산해서 서버에 설치할 라이브러리를 줄였습니다)
# ─────────────────────────────────────────────
def sma(values, n):
    return sum(values[-n:]) / n


def stdev(values, n):
    m = sma(values, n)
    return (sum((v - m) ** 2 for v in values[-n:]) / n) ** 0.5


def rsi(values, n=14):
    gains, losses = [], []
    for a, b in zip(values[:-1], values[1:]):
        gains.append(max(b - a, 0))
        losses.append(max(a - b, 0))
    avg_g = sum(gains[:n]) / n
    avg_l = sum(losses[:n]) / n
    for g, l in zip(gains[n:], losses[n:]):        # 와일더 방식 평균
        avg_g = (avg_g * (n - 1) + g) / n
        avg_l = (avg_l * (n - 1) + l) / n
    if avg_l == 0:
        return 100.0
    return 100 - 100 / (1 + avg_g / avg_l)


def get_signal(closes, s):
    """완성된 봉의 종가 목록을 받아 "buy", "sell", None 중 하나를 돌려줍니다."""
    if s["type"] == "golden_cross":
        prev_s, prev_l = sma(closes[:-1], s["short"]), sma(closes[:-1], s["long"])
        now_s, now_l = sma(closes, s["short"]), sma(closes, s["long"])
        if prev_s <= prev_l and now_s > now_l:
            return "buy"
        if prev_s >= prev_l and now_s < now_l:
            return "sell"

    elif s["type"] == "rsi":
        r = rsi(closes, s["period"])
        if r <= s["buy"]:
            return "buy"
        if r >= s["sell"]:
            return "sell"

    elif s["type"] == "bollinger":
        mid = sma(closes, s["period"])
        sd = stdev(closes, s["period"])
        upper, lower = mid + s["k"] * sd, mid - s["k"] * sd
        price = closes[-1]
        if price > upper:
            return "buy"
        if s["sell"] == "middle" and price < mid:
            return "sell"
        if s["sell"] == "lower" and price <= lower:
            return "sell"
    return None


# ─────────────────────────────────────────────
# ⑥ 가상 매수, 매도 (업비트에는 시세 조회만 합니다)
# ─────────────────────────────────────────────
def buy(state, symbol, price, reason):
    budget = min(START_CAPITAL * MAX_WEIGHT, state["cash"])
    if budget < 5_000:                              # 업비트 최소 주문금액
        return None
    fill = price * (1 + SLIPPAGE)
    qty = budget * (1 - FEE) / fill
    state["cash"] -= budget
    state["holdings"][symbol] = {"qty": qty, "avg": fill}
    return {"side": "매수", "fill": fill, "qty": qty, "amount": budget, "pnl": None, "reason": reason}


def sell(state, symbol, price, reason):
    h = state["holdings"].pop(symbol)
    fill = price * (1 - SLIPPAGE)
    amount = h["qty"] * fill * (1 - FEE)
    state["cash"] += amount
    pnl = (fill / h["avg"] - 1) * 100
    return {"side": "매도", "fill": fill, "qty": h["qty"], "amount": amount, "pnl": pnl, "reason": reason}


def record(state, symbol, t):
    state["trade_no"] += 1
    coin = symbol.split("/")[0]
    write_trade_log([f"{now():%Y-%m-%d %H:%M}", symbol, t["side"], round(t["fill"]),
                     round(t["qty"], 8), round(t["amount"]),
                     "" if t["pnl"] is None else round(t["pnl"], 2), t["reason"], round(state["cash"])])
    pnl = "" if t["pnl"] is None else f" ({t['pnl']:+.2f}%)"
    log(f"[시뮬레이션] {symbol} {t['side']} {t['amount']:,.0f}원 @ {t['fill']:,.0f}{pnl} {t['reason']}")
    message = (f"📊 [자동매매 알림]\n"
               f"종목: {symbol}\n"
               f"판단: {t['side']}{pnl}\n"
               f"가격: {t['fill']:,.0f}원\n"
               f"수량: {t['qty']:.4f} {coin}\n"
               f"잔고: {state['cash']:,.0f}원")
    return f"{symbol}-{state['trade_no']}", message


# ─────────────────────────────────────────────
# ⑦ 비상 정지 (카카오톡이 실패해도 정지는 반드시 유지)
# ─────────────────────────────────────────────
def emergency_stop(total):
    if HALT_FILE.exists():
        log("⛔ 비상 정지 상태입니다. 매매하지 않고 종료합니다. (재개하려면 trading_halted.json 삭제)")
        return True
    if total > START_CAPITAL * EMERGENCY_RATIO:
        return False
    # (1) 먼저 정지 표시 파일부터 저장
    HALT_FILE.write_text(json.dumps({"halted_at": now().isoformat(timespec="seconds"),
                                     "total_asset": round(total)}, ensure_ascii=False), encoding="utf-8")
    rate = (total / START_CAPITAL - 1) * 100
    log(f"⛔ 손실 한도 도달 (총자산 {total:,.0f}원, {rate:+.1f}%). 매매를 중단합니다.")
    # (2) 그다음 카카오톡 비상 알림
    kakao("EMERGENCY-STOP", f"🚨 [비상 정지]\n총자산 {total:,.0f}원 ({rate:+.1f}%)\n"
                            f"손실 한도에 도달해 자동매매를 멈췄습니다.\n"
                            f"재개하려면 trading_halted.json 을 삭제하세요.")
    return True


# ─────────────────────────────────────────────
# ⑧ 메인 실행 (크론탭이 30분마다 이 부분을 한 번 실행)
# ─────────────────────────────────────────────
def fetch_closes(exchange, symbol, timeframe, need):
    candles = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=need + 1)
    return [c[4] for c in candles[:-1]]             # 아직 진행 중인 마지막 봉은 제외


def main():
    if KAKAO_READY:
        flush_pending()                             # 지난번에 못 보낸 알림 다시 보내기

    exchange = ccxt.upbit({"enableRateLimit": True})   # API 키 없이 시세 조회만
    state = load_state()

    # 현재가 조회
    prices = {}
    for symbol in STRATEGIES:
        prices[symbol] = exchange.fetch_ticker(symbol)["last"]

    total = state["cash"] + sum(h["qty"] * prices[s] for s, h in state["holdings"].items())
    if emergency_stop(total):
        return

    alerts = []
    for symbol, s in STRATEGIES.items():
        try:
            price = prices[symbol]
            held = state["holdings"].get(symbol)

            # 손절, 익절이 전략 신호보다 먼저
            if held:
                change = price * (1 - SLIPPAGE) / held["avg"] - 1
                if change <= STOP_LOSS:
                    alerts.append(record(state, symbol, sell(state, symbol, price, "손절 발동")))
                    continue
                if change >= TAKE_PROFIT:
                    alerts.append(record(state, symbol, sell(state, symbol, price, "익절 도달")))
                    continue

            need = max(s.get("long", 0), s.get("period", 0)) + 100
            closes = fetch_closes(exchange, symbol, s["timeframe"], need)
            signal = get_signal(closes, s)

            if signal == "buy" and not held:
                t = buy(state, symbol, price, "매수 신호")
                if t:
                    alerts.append(record(state, symbol, t))
            elif signal == "sell" and held:
                alerts.append(record(state, symbol, sell(state, symbol, price, "매도 신호")))
        except Exception as e:                      # 한 종목에서 오류가 나도 나머지는 계속
            log(f"{symbol} 처리 중 오류 {e}")
        time.sleep(0.2)

    save_state(state)                               # 잔고 저장을 마친 뒤에 알림 전송
    for trade_id, message in alerts:
        kakao(trade_id, message)

    total = state["cash"] + sum(h["qty"] * prices[s] for s, h in state["holdings"].items())
    holding = ", ".join(s.split("/")[0] for s in state["holdings"]) or "없음"
    rate = (total / START_CAPITAL - 1) * 100
    log(f"💰 총자산 {total:,.0f}원 ({rate:+.2f}%) 현금 {state['cash']:,.0f}원 보유 {holding}")

    # 매매가 없었던 실행도 카톡으로 상태 보고 (봇이 살아 있는지 휴대폰으로 확인)
    if not alerts and REPORT_EVERY_RUN and KAKAO_READY:
        try:
            send_to_kakao(f"⏰ [자동매매 점검] {now():%Y-%m-%d %H:%M}\n"
                          f"매매 실적 없음\n"
                          f"총자산: {total:,.0f}원 ({rate:+.2f}%)\n"
                          f"보유 종목: {holding}")
        except Exception as e:
            log(f"카카오톡 상태 보고 실패 {e}")


if __name__ == "__main__":
    log("💎 AI 자동매매 봇 실행 (시뮬레이션 모드, 실제 주문 없음)")
    main()
