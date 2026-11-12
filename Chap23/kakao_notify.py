# kakao_notify.py
# 23장 카카오톡 ‘나와의 채팅방’ 알림 모듈 (구글 클라우드 자동매매용)
#
# 사용법 (cryptoenv 가상환경 안에서)
#   1) python kakao_notify.py setup     처음 한 번, 카카오 인증 후 kakao_token.json 생성
#   2) python kakao_notify.py test      테스트 메시지 보내기
#   3) python kakao_notify.py refresh   토큰을 강제로 갱신한 뒤 테스트 메시지 보내기
#
# auto_trade.py 에서는 다음 두 함수만 가져다 씁니다.
#   send_to_kakao(message)              메시지 한 건 보내기 (성공 True, 실패 False)
#   notify_trade(trade_id, message)     같은 거래는 한 번만 보내기 (중복 방지)

import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlencode, urlparse, parse_qs

import requests

# 크론탭은 홈 폴더에서 실행되므로, 파일 위치를 이 파이썬 파일이 있는 폴더 기준으로 고정합니다.
BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "kakao_config.json"    # REST API 키, 리다이렉트 URI, (켜 둔 경우) 클라이언트 시크릿
TOKEN_FILE = BASE_DIR / "kakao_token.json"      # 액세스 토큰, 리프레시 토큰 (setup 이 자동 생성)
SENT_FILE = BASE_DIR / "kakao_sent.json"        # 이미 알림을 보낸 거래 ID 목록 (중복 방지)
PENDING_FILE = BASE_DIR / "kakao_pending.json"  # 전송에 실패해 다음 실행 때 다시 보낼 메시지

AUTH_HOST = "https://kauth.kakao.com"
API_HOST = "https://kapi.kakao.com"
TIMEOUT = 10            # 카카오 서버가 10초 안에 답이 없으면 포기 (매매가 멈추지 않도록)
REFRESH_MARGIN = 600    # 만료 10분 전이면 미리 갱신


# ── 파일 읽기, 쓰기 도우미 ─────────────────────────────

def _load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _save_json(path, data):
    # 임시 파일에 먼저 쓰고 바꿔치기 (저장 도중 꺼져도 파일이 깨지지 않게)
    tmp = Path(str(path) + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)   # 나만 읽을 수 있게 권한 제한
    except OSError:
        pass


def _log(msg):
    print(f"[kakao {time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}")


def _load_config():
    cfg = _load_json(CONFIG_FILE, None)
    if not cfg or not cfg.get("rest_api_key"):
        raise RuntimeError("kakao_config.json 이 없거나 rest_api_key 가 비어 있습니다.")
    return cfg


# ── 1단계. 처음 한 번 토큰 발급 ─────────────────────────

def setup():
    cfg = _load_config()
    auth_url = f"{AUTH_HOST}/oauth/authorize?" + urlencode({
        "client_id": cfg["rest_api_key"],
        "redirect_uri": cfg["redirect_uri"],
        "response_type": "code",
        "scope": "talk_message",
    })
    print("\n① 아래 주소를 복사해 내 PC 브라우저에 붙여넣고 카카오 로그인, 동의를 진행하세요.\n")
    print(auth_url)
    print("\n② ‘사이트에 연결할 수 없음’ 화면이 떠도 정상입니다.")
    print("   그때 브라우저 주소창의 주소 전체를 복사해 아래에 붙여넣으세요.\n")
    pasted = input("주소 붙여넣기 > ").strip()

    # 주소 전체를 붙여넣어도, code 값만 붙여넣어도 되게 처리
    code = parse_qs(urlparse(pasted).query).get("code", [pasted])[0]

    data = {
        "grant_type": "authorization_code",
        "client_id": cfg["rest_api_key"],
        "redirect_uri": cfg["redirect_uri"],
        "code": code,
    }
    if cfg.get("client_secret"):
        data["client_secret"] = cfg["client_secret"]

    res = requests.post(f"{AUTH_HOST}/oauth/token", data=data, timeout=TIMEOUT)
    body = res.json()
    if "access_token" not in body:
        print("토큰 발급 실패", body)
        sys.exit(1)

    now = int(time.time())
    _save_json(TOKEN_FILE, {
        "access_token": body["access_token"],
        "refresh_token": body["refresh_token"],
        "access_expires_at": now + int(body.get("expires_in", 0)),
        "refresh_expires_at": now + int(body.get("refresh_token_expires_in", 0)),
    })
    print(f"\n✅ 토큰 발급 완료. {TOKEN_FILE.name} 파일에 저장했습니다.")


# ── 2단계. 토큰 자동 갱신 ─────────────────────────────

def _refresh(tokens, cfg):
    data = {
        "grant_type": "refresh_token",
        "client_id": cfg["rest_api_key"],
        "refresh_token": tokens["refresh_token"],
    }
    if cfg.get("client_secret"):
        data["client_secret"] = cfg["client_secret"]

    res = requests.post(f"{AUTH_HOST}/oauth/token", data=data, timeout=TIMEOUT)
    body = res.json()
    if "access_token" not in body:
        raise RuntimeError(f"토큰 갱신 실패 {body}  (setup 을 다시 실행하세요)")

    now = int(time.time())
    tokens["access_token"] = body["access_token"]
    tokens["access_expires_at"] = now + int(body.get("expires_in", 0))
    # 리프레시 토큰은 만료 1개월 전부터 새로 내려옵니다. 내려오면 반드시 교체 저장.
    if "refresh_token" in body:
        tokens["refresh_token"] = body["refresh_token"]
        tokens["refresh_expires_at"] = now + int(body.get("refresh_token_expires_in", 0))
        _log("리프레시 토큰이 새로 발급되어 교체했습니다.")
    _save_json(TOKEN_FILE, tokens)
    _log("액세스 토큰을 갱신했습니다.")
    return tokens


def get_access_token(force_refresh=False):
    cfg = _load_config()
    tokens = _load_json(TOKEN_FILE, None)
    if not tokens:
        raise RuntimeError("kakao_token.json 이 없습니다. 먼저 python kakao_notify.py setup 을 실행하세요.")
    if force_refresh or time.time() > tokens.get("access_expires_at", 0) - REFRESH_MARGIN:
        tokens = _refresh(tokens, cfg)
    return tokens["access_token"]


# ── 3단계. 메시지 보내기 ─────────────────────────────

def _post_memo(access_token, message):
    template = {
        "object_type": "text",
        "text": message[:200],   # 텍스트 템플릿은 최대 200자
        "link": {"web_url": "https://www.upbit.com", "mobile_web_url": "https://www.upbit.com"},
        "button_title": "업비트 열기",
    }
    return requests.post(
        f"{API_HOST}/v2/api/talk/memo/default/send",
        headers={"Authorization": f"Bearer {access_token}"},
        data={"template_object": json.dumps(template, ensure_ascii=False)},
        timeout=TIMEOUT,
    )


def send_to_kakao(message):
    """나와의 채팅방으로 메시지 한 건 보내기. 어떤 오류가 나도 예외를 밖으로 던지지 않습니다."""
    try:
        res = _post_memo(get_access_token(), message)
        if res.status_code == 401:
            # 토큰이 서버에서 먼저 만료된 경우, 강제 갱신 후 한 번만 다시 시도
            res = _post_memo(get_access_token(force_refresh=True), message)
        if res.status_code == 200 and res.json().get("result_code") == 0:
            _log("카카오톡 전송 성공")
            return True
        _log(f"카카오톡 전송 실패 {res.status_code} {res.text[:200]}")
    except Exception as e:
        _log(f"카카오톡 전송 오류 {e}")
    return False


# ── 4단계. 거래 알림 (중복 방지 + 실패 시 다음 실행 때 재전송) ──

def notify_trade(trade_id, message):
    sent = _load_json(SENT_FILE, [])
    if trade_id in sent:
        _log(f"이미 보낸 알림이라 건너뜀 ({trade_id})")
        return True
    ok = send_to_kakao(message)
    if ok:
        sent = (sent + [trade_id])[-500:]   # 최근 500건만 기억
        _save_json(SENT_FILE, sent)
    else:
        pending = _load_json(PENDING_FILE, {})
        pending[trade_id] = message
        _save_json(PENDING_FILE, pending)
        _log(f"다음 실행 때 다시 보냅니다 ({trade_id})")
    return ok


def flush_pending():
    """지난 실행에서 못 보낸 알림을 다시 보냅니다. auto_trade.py 시작 부분에서 한 번 호출."""
    pending = _load_json(PENDING_FILE, {})
    if not pending:
        return
    _save_json(PENDING_FILE, {})
    for trade_id, message in pending.items():
        notify_trade(trade_id, message)


# ── 터미널에서 직접 실행할 때 ───────────────────────────

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "test"
    if cmd == "setup":
        setup()
    elif cmd == "refresh":
        get_access_token(force_refresh=True)
        print("갱신 후 전송", "성공 ✅" if send_to_kakao("🔄 토큰 갱신 테스트 메시지입니다.") else "실패 ❌")
    else:
        print("전송", "성공 ✅" if send_to_kakao("🎉 첫 카카오톡 자동알림 성공!\n\n앞으로 AI 분석 결과, 주식 시세, 업무 알림이 모두 이쪽으로 옵니다.") else "실패 ❌")
