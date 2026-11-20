"""반영·되돌리기가 끝나면 메신저(웹훅)로 한 줄 알리기.

서버 연결마다 웹훅 URL 과 형식(Slack·Mattermost·Teams 는 text, 잔디는 body, Discord 는 content, 직접 입력)을 두고,
{키: "메시지"} JSON 하나를 POST 한다. 알림이 실패해도 반영 결과는 바뀌지 않으므로 호출하는 쪽은 경고만 보여 준다.
"""
from __future__ import annotations

import getpass
from typing import List, Optional, Sequence

from shipkit.config import NotifyConfig

TIMEOUT = 5.0   # 초. 메신저가 느려도 반영 결과 표시를 오래 막지 않게
MAX_REQUESTS = 20   # 변경 내역에 붙일 MR·PR 최대 개수 (넘으면 '외 N개')
MAX_FILES = 15      # 변경 내역에 붙일 파일 최대 개수

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
            files: Optional[int] = None, sender: Optional[str] = None, details: Sequence[str] = (),
            health=None) -> str:
    """메신저에 보낼 글. 첫 줄 예: '✅ erp 운영에 2026-11-04-1430 반영, 파일 12개 (minakim)'.

    action 은 마지막으로 실행한 단계(apply / rollback, 실패했을 때는 backup 일 수도). details(detail_lines) 가 있으면
    반영(되돌리기 말고) 알림의 아래 줄로 붙는다. health(remote.HealthResult) 는 반영 후 확인 결과: 실패면 첫 줄이
    ⚠️ '반영했지만 확인 실패' 가 되고 둘째 줄에 요약이 붙는다."""
    sender = who() if sender is None else sender
    suffix = f" ({sender})" if sender else ""
    if action == "rollback":
        if ok:
            return f"↩️ {project} {server} 되돌리기: {package} (반영 전 상태로){suffix}"
        return f"❌ {project} {server} 되돌리기 실패 (종료 코드 {code}): {package}{suffix}"
    lines: List[str] = []
    if ok:
        count = f", 파일 {files}개" if files is not None else ""
        if health is not None and not health.ok:
            lines.append(f"⚠️ {project} {server}에 {package} 반영했지만 확인 실패{count}{suffix}")
        else:
            lines.append(f"✅ {project} {server}에 {package} 반영{count}{suffix}")
        if health is not None:
            lines.append(f"확인: {health.summary()}")
    else:
        step = ACTION_TEXT.get(action, action)
        lines.append(f"❌ {project} {server} 반영 실패 ({step}, 종료 코드 {code}): {package}{suffix}")
    return "\n".join(lines + list(details))


def detail_lines(record, detail: str) -> List[str]:
    """알림 아래에 붙일 변경 내역 (설정 notify.detail). record 는 history.PackageRecord (없으면 빈 목록).

    requests: MR·PR 한 줄씩 '• !612 제목 (작성자)'. MR 없이 만든 패키지면 패키지 이름(설명) 한 줄.
    files: 거기에 '파일: a, b, … 외 N개' 한 줄 더 (지울 파일은 '삭제 ' 를 앞에)."""
    if record is None or detail not in ("requests", "files"):
        return []
    lines: List[str] = []
    requests = list(record.requests or [])
    if requests:
        for r in requests[:MAX_REQUESTS]:
            author = str(r.get("author") or "").strip()
            lines.append(f"• {r.get('ref', '')} {str(r.get('title') or '').strip()}".rstrip() + (f" ({author})" if author else ""))
        if len(requests) > MAX_REQUESTS:
            lines.append(f"• … 외 {len(requests) - MAX_REQUESTS}개")
    elif record.label or record.description:
        lines.append(f"• {record.label or record.description}")
    if detail == "files":
        names = list(record.applied or []) + [f"삭제 {d}" for d in (record.deletes or [])]
        if names:
            more = f" … 외 {len(names) - MAX_FILES}개" if len(names) > MAX_FILES else ""
            lines.append("파일: " + ", ".join(names[:MAX_FILES]) + more)
    return lines


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
