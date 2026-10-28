"""sync 모드: 빌드 결과 폴더 전체를 배포 계획으로 만든다 (파일 복사 없이 계획만).

Vue/React 처럼 결과 파일 이름이 빌드마다 바뀌는(app.3f9a.js) 프로젝트는 소스 1개와 결과 파일이
1:1 로 대응하지 않는다. 그래서 빌드 결과 전체를 패키지에 담고, 실제로 바뀐 파일만 반영하는 일과
prune 폴더의 옛 파일 정리는 서버 스크립트가 서버의 현재 상태와 비교해서 한다.
"""
from __future__ import annotations

import posixpath
from pathlib import Path
from typing import List, Optional

from shipkit.config import ProjectConfig
from shipkit.mapper import SENSITIVE_MESSAGE, STALE_TOLERANCE
from shipkit.models import Artifact, ChangeType, Entry, FileChange, SourceInfo, Status


def build_root(project: ProjectConfig) -> Path:
    return project.root / project.build_dir


def plan_sync(project: ProjectConfig, changes: List[FileChange], source: SourceInfo) -> List[Entry]:
    """빌드 결과 파일마다 Entry 하나를 만든다. 변경 목록은 빌드 누락 검사에만 쓴다."""
    root = build_root(project)
    if not root.is_dir():
        source.warnings.append(f"빌드 결과 폴더가 없습니다: {root} (빌드를 먼저 하세요)")
        return []

    entries: List[Entry] = []
    newest_build = 0.0
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        rel = path.relative_to(root).as_posix()
        entry = Entry(FileChange(f"{project.build_dir}/{rel}", ChangeType.MODIFIED, "build"))
        entries.append(entry)
        if project.is_excluded(rel):
            entry.status = Status.SKIPPED
            entry.messages.append("제외 패턴에 해당")
            continue
        if project.is_sensitive(rel):
            entry.fail(SENSITIVE_MESSAGE)
            continue
        target = posixpath.join(project.dest, rel) if project.dest else rel
        entry.artifacts.append(Artifact(target, path))
        newest_build = max(newest_build, path.stat().st_mtime)

    if not any(e.artifacts for e in entries):
        source.warnings.append(f"빌드 결과 폴더에 배포할 파일이 없습니다: {root}")
        return entries

    stale = _stale_sources(project, changes, newest_build)
    if stale:
        shown = ", ".join(stale[:5]) + (f" 외 {len(stale) - 5}개" if len(stale) > 5 else "")
        source.warnings.append(f"빌드 결과보다 최근에 수정된 소스가 있습니다 - 빌드 누락 의심 (다시 빌드하세요): {shown}")
    return entries


def _stale_sources(project: ProjectConfig, changes: List[FileChange], newest_build: float) -> List[str]:
    stale = []
    prefix = project.build_dir + "/"
    for ch in changes:
        if ch.change == ChangeType.DELETED or ch.path.startswith(prefix):
            continue
        src = project.root / ch.path
        if src.is_file() and src.stat().st_mtime > newest_build + STALE_TOLERANCE:
            stale.append(ch.path)
    return stale


def summarize(entries: List[Entry]) -> Optional[str]:
    files = sum(1 for e in entries if e.artifacts)
    return f"빌드 결과 {files}개 파일" if files else None
