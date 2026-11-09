"""반영·되돌리기가 끝나면 메신저(웹훅)로 한 줄 알리기.

서버 연결마다 웹훅 URL 과 형식(Slack·Mattermost·Teams 는 text, 잔디는 body, Discord 는 content, 직접 입력)을 두고,
{키: "메시지"} JSON 하나를 POST 한다. 알림이 실패해도 반영 결과는 바뀌지 않으므로 호출하는 쪽은 경고만 보여 준다.
"""
from __future__ import annotations

import getpass
from typing import Optional

from shipkit.config import NotifyConfig

TIMEOUT = 5.0   # 초. 메신저가 느려도 반영 결과 표시를 오래 막지 않게

ACTION_TEXT = {"apply": "반영", "rollback": "되돌리기", "check": "확인", "backup": "백업"}


class NotifyError(Exception):
    pass


def who() -> str:
    """알림에 붙일 보낸 사람 (이 PC 의 로그인 이름). 알 수 없으면 빈 글."""
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001 - 환경에 따라 어떤 오류든 날 수 있다
        return ""


def compose(project: str, server: str, action: str, package: str, ok: bool, code: int = 0,
            files: Optional[int] = None, sender: Optional[str] = None) -> str:
    """메신저에 보낼 한 줄. 예: '✅ erp 운영에 2026-11-04-1430 반영, 파일 12개 (minakim)'.

    action 은 마지막으로 실행한 단계(apply / rollback, 실패했을 때는 backup 일 수도)."""
    sender = who() if sender is None else sender
    suffix = f" ({sender})" if sender else ""
    if action == "rollback":
        if ok:
            return f"↩️ {project} {server} 되돌리기: {package} (반영 전 상태로){suffix}"
        return f"❌ {project} {server} 되돌리기 실패 (종료 코드 {code}): {package}{suffix}"
    if ok:
        count = f", 파일 {files}개" if files is not None else ""
        return f"✅ {project} {server}에 {package} 반영{count}{suffix}"
    step = ACTION_TEXT.get(action, action)
    return f"❌ {project} {server} 반영 실패 ({step}, 종료 코드 {code}): {package}{suffix}"


def test_message(project: str, server: str, sender: Optional[str] = None) -> str:
    sender = who() if sender is None else sender
    return f"🔔 ShipKit 알림 시험: {project} {server} 서버의 반영·되돌리기 결과를 여기로 보냅니다" + (f" ({sender})" if sender else "")


def payload(cfg: NotifyConfig, text: str) -> dict:
    return {cfg.field: text}


def send(cfg: NotifyConfig, text: str, timeout: float = TIMEOUT) -> None:
    """웹훅으로 보낸다. 실패하면 NotifyError (네트워크·HTTP 오류 모두)."""
    import requests
    try:
        r = requests.post(cfg.url, json=payload(cfg, text), timeout=timeout)
    except requests.RequestException as e:
        raise NotifyError(f"메신저 알림을 보내지 못했습니다 ({cfg.label}): {e}") from e
    if r.status_code >= 400:
        body = (r.text or "").strip().replace("\n", " ")[:200]
        raise NotifyError(f"메신저가 알림을 거부했습니다 ({cfg.label}, HTTP {r.status_code}): {body}")
