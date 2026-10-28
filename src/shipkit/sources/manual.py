"""직접 입력한 파일 목록 파싱.

한 줄에 경로 하나. 'git diff --name-status' 출력을 그대로 붙여넣어도 된다.
  src/main/java/a/B.java          -> 수정(M)으로 처리
  D<TAB>src/main/webapp/old.jsp    -> 삭제
  R100<TAB>old/path<TAB>new/path   -> old 삭제 + new 추가
빈 줄과 '#' 으로 시작하는 줄은 무시한다.
"""
from __future__ import annotations

import re
from typing import Iterable, List, Tuple

from shipkit.models import ChangeType, FileChange, normalize_repo_path

_STATUS_RE = re.compile(r"^(A|M|D|T|R\d*|C\d*)\s+(.+)$")


def parse_lines(lines: Iterable[str], origin: str = "manual") -> Tuple[List[FileChange], List[str]]:
    changes: List[FileChange] = []
    errors: List[str] = []
    for lineno, line in enumerate(lines, 1):
        text = line.strip().lstrip("﻿")
        if not text or text.startswith("#"):
            continue
        try:
            changes.extend(_parse_line(text, origin))
        except ValueError as e:
            errors.append(f"{lineno}행: {e}")
    return changes, errors


def _parse_line(text: str, origin: str) -> List[FileChange]:
    m = _STATUS_RE.match(text)
    if not m:
        return [FileChange(normalize_repo_path(text), ChangeType.MODIFIED, origin)]
    status, rest = m.group(1), m.group(2)
    if status[0] in "RC":
        parts = [p for p in re.split(r"\t+", rest) if p.strip()]
        if len(parts) != 2:
            raise ValueError(f"이름변경(R) 줄은 '이전경로<TAB>새경로' 형식이어야 합니다: {text!r}")
        old, new = (normalize_repo_path(p) for p in parts)
        result = [FileChange(new, ChangeType.ADDED, origin)]
        if status[0] == "R":
            result.insert(0, FileChange(old, ChangeType.DELETED, origin))
        return result
    change = {"A": ChangeType.ADDED, "D": ChangeType.DELETED}.get(status, ChangeType.MODIFIED)
    return [FileChange(normalize_repo_path(rest), change, origin)]
