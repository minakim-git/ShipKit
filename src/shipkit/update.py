"""새 버전 확인: 공유 폴더(또는 URL)에 둔 version.txt 를 읽어 지금 버전과 비교한다.

사내 배포용이라 GitHub 같은 바깥 서비스는 보지 않는다. 배포 담당자가 dist 폴더의 ShipKit.exe 와 version.txt 를
공유 폴더에 복사해 두면, 각 PC 의 앱이 시작할 때 그 폴더의 version.txt 를 읽고 새 버전이면 왼쪽 아래에 알린다.
version.txt: 첫 줄이 버전(예: 0.11.0), 나머지 줄은 안내문(선택).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

from shipkit import __version__

VERSION_FILE = "version.txt"
_NUMBER = re.compile(r"\d+")


class UpdateError(Exception):
    """확인 위치를 읽지 못했거나 version.txt 내용이 이상할 때."""


@dataclass
class UpdateInfo:
    version: str
    location: str      # 설정한 확인 위치 (폴더 또는 URL). 알림을 누르면 여기를 연다
    notes: str = ""    # version.txt 둘째 줄부터

    @property
    def is_url(self) -> bool:
        return is_url(self.location)


def is_url(location: str) -> bool:
    return location.lower().startswith(("http://", "https://"))


def parse_version(text: str) -> Tuple[int, ...]:
    """'0.11.0', 'v0.11', '0.11.0-beta' → (0, 11, 0). 숫자가 없으면 빈 튜플."""
    head = text.strip().lstrip("vV").split("-")[0].split("+")[0]
    return tuple(int(n) for n in _NUMBER.findall(head))


def is_newer(candidate: str, current: str = __version__) -> bool:
    a, b = parse_version(candidate), parse_version(current)
    if not a:
        return False
    length = max(len(a), len(b))
    return a + (0,) * (length - len(a)) > b + (0,) * (length - len(b))


def read_version_text(location: str, timeout: float = 3.0) -> str:
    """확인 위치의 version.txt 내용. 폴더(로컬·공유)면 그 안의 version.txt, URL 이면 받아 온다.
    위치가 version.txt 파일 자체(또는 그 URL)를 가리켜도 된다."""
    if is_url(location):
        import requests
        url = location if location.lower().endswith(".txt") else location.rstrip("/") + "/" + VERSION_FILE
        try:
            r = requests.get(url, timeout=timeout)
            r.raise_for_status()
        except requests.RequestException as e:
            raise UpdateError(f"새 버전 확인 위치를 읽지 못했습니다: {url}\n{e}") from e
        return r.text
    path = Path(location)
    if path.suffix.lower() != ".txt":
        path = path / VERSION_FILE
    try:
        return path.read_text(encoding="utf-8-sig")
    except OSError as e:
        raise UpdateError(f"새 버전 확인 위치를 읽지 못했습니다: {path}\n{e}") from e


def check(location: str, current: str = __version__, timeout: float = 3.0) -> Optional[UpdateInfo]:
    """지금 버전보다 새 버전이 있으면 UpdateInfo, 아니면 None. 읽지 못하면 UpdateError."""
    lines = [ln.strip() for ln in read_version_text(location, timeout).splitlines()]
    body = [ln for ln in lines if ln]
    if not body or not parse_version(body[0]):
        raise UpdateError(f"version.txt 첫 줄이 버전이 아닙니다: {body[0] if body else '(비어 있음)'}")
    version = body[0].lstrip("vV")
    if not is_newer(version, current):
        return None
    return UpdateInfo(version=version, location=location, notes="\n".join(body[1:]).strip())


def write_version_file(path: Path, version: str = __version__, notes: str = "") -> Path:
    """빌드할 때 dist 에 version.txt 를 만든다 (exe 와 같이 공유 폴더에 복사)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(version + ("\n" + notes.strip() if notes.strip() else "") + "\n", encoding="utf-8")
    return path
