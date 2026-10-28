"""변경 파일과 패키징 결과를 표현하는 데이터 모델."""
from __future__ import annotations

import posixpath
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, Iterable, List, Optional


class ChangeType(str, Enum):
    ADDED = "A"
    MODIFIED = "M"
    DELETED = "D"

    @property
    def label(self) -> str:
        return {"A": "추가", "M": "수정", "D": "삭제"}[self.value]


class Status(str, Enum):
    OK = "성공"
    WARN = "경고"
    ERROR = "실패"
    SKIPPED = "제외"


def normalize_repo_path(raw: str) -> str:
    """저장소 기준 상대 경로를 'a/b/c.java' 형태로 정규화한다.

    절대 경로나 저장소 밖을 가리키는 경로('..')는 거부한다.
    """
    p = raw.strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    if not p:
        raise ValueError("빈 경로입니다.")
    if p.startswith("/") or (len(p) > 1 and p[1] == ":"):
        raise ValueError(f"저장소 기준 상대 경로가 아닙니다: {raw!r}")
    norm = posixpath.normpath(p)
    if norm == ".." or norm.startswith("../"):
        raise ValueError(f"저장소 밖을 가리키는 경로입니다: {raw!r}")
    return norm


@dataclass(frozen=True)
class FileChange:
    path: str
    change: ChangeType
    origin: str = ""  # 예: "!12" (MR), "git", "manual"


# (이전 상태, 새 상태) -> 합쳐진 상태. None 은 "변경 없음"으로 상쇄됨을 뜻한다.
_MERGE_TABLE: Dict[tuple, Optional[ChangeType]] = {
    (ChangeType.ADDED, ChangeType.ADDED): ChangeType.ADDED,
    (ChangeType.ADDED, ChangeType.MODIFIED): ChangeType.ADDED,
    (ChangeType.ADDED, ChangeType.DELETED): None,
    (ChangeType.MODIFIED, ChangeType.ADDED): ChangeType.MODIFIED,
    (ChangeType.MODIFIED, ChangeType.MODIFIED): ChangeType.MODIFIED,
    (ChangeType.MODIFIED, ChangeType.DELETED): ChangeType.DELETED,
    (ChangeType.DELETED, ChangeType.ADDED): ChangeType.MODIFIED,
    (ChangeType.DELETED, ChangeType.MODIFIED): ChangeType.MODIFIED,
    (ChangeType.DELETED, ChangeType.DELETED): ChangeType.DELETED,
}


def merge_changes(batches: Iterable[List[FileChange]]) -> List[FileChange]:
    """시간순으로 정렬된 변경 묶음(MR 등)을 하나의 변경 목록으로 합친다.

    같은 파일이 여러 번 바뀌면 최종 상태만 남긴다. 추가 후 삭제된 파일은 사라진다.
    """
    merged: Dict[str, FileChange] = {}
    for batch in batches:
        for ch in batch:
            prev = merged.get(ch.path)
            if prev is None:
                merged[ch.path] = ch
                continue
            result = _MERGE_TABLE[(prev.change, ch.change)]
            if result is None:
                del merged[ch.path]
            else:
                origin = prev.origin if prev.origin == ch.origin else f"{prev.origin},{ch.origin}"
                merged[ch.path] = FileChange(ch.path, result, origin.strip(","))
    return sorted(merged.values(), key=lambda c: c.path)


@dataclass
class Artifact:
    """서버에 올라갈 파일 하나. target 은 서버 배포 경로(server_path) 기준 상대 경로."""
    target: str
    local_file: Path


@dataclass
class Entry:
    """변경 파일 하나에 대한 처리 결과."""
    change: FileChange
    status: Status = Status.OK
    artifacts: List[Artifact] = field(default_factory=list)
    deletes: List[str] = field(default_factory=list)  # 서버에서 삭제할 상대 경로
    messages: List[str] = field(default_factory=list)

    def warn(self, msg: str) -> None:
        self.messages.append(msg)
        if self.status == Status.OK:
            self.status = Status.WARN

    def fail(self, msg: str) -> None:
        self.messages.append(msg)
        self.status = Status.ERROR


@dataclass
class SourceInfo:
    """변경 목록을 어디서 가져왔는지 (보고서용)."""
    kind: str  # "gitlab-mr" | "git-diff" | "manual"
    description: str
    merge_requests: List[dict] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
