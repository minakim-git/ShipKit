"""패키지 만들기 공통 흐름: 변경 수집 -> 배포 계획. 화면 출력이 없어서 CLI·대화형·GUI 가 함께 쓴다.

    changes, source = collect_git(project, "v1.2..HEAD")     # 또는 collect_requests / collect_lines / collect_build
    entries = plan(project, changes, source)                  # 경고·알림은 source 에 쌓인다
    result = packager.build_package(project, entries, source, ...)
"""
from __future__ import annotations

import logging
from typing import List, Sequence, Tuple

from shipkit import mapper, sync
from shipkit.config import ProjectConfig
from shipkit.models import Entry, FileChange, SourceInfo, Status, merge_changes
from shipkit.sources import git_local, manual
from shipkit.sources.providers import Provider

log = logging.getLogger(__name__)


class InputError(ValueError):
    """사용자가 입력한 파일 목록 등에 잘못된 줄이 있을 때."""


# ---------------------------------------------------------------------- 변경 수집
def collect_build(project: ProjectConfig) -> Tuple[List[FileChange], SourceInfo]:
    """sync 모드: 변경 목록 없이 빌드 결과 전체."""
    return [], SourceInfo(kind="build", description=f"빌드 결과 전체 ({project.build_dir}, 변경 목록 없음)")


def collect_git(project: ProjectConfig, rev_range: str) -> Tuple[List[FileChange], SourceInfo]:
    changes, desc = git_local.collect_from_git(project.root, rev_range)
    source = SourceInfo(kind="git-diff", description=desc)
    _, head = git_local.parse_range(rev_range)
    head_sha = git_local.resolve_commit(project.root, head)
    local_sha = git_local.resolve_commit(project.root, "HEAD")
    if head_sha and local_sha and head_sha != local_sha:
        source.warnings.append(
            f"로컬 HEAD({local_sha[:9]})가 범위 끝 {head}({head_sha[:9]})와 다릅니다. "
            "복사되는 파일은 지금 작업 폴더(현재 브랜치)의 파일과 빌드 결과입니다."
        )
    return changes, source


def latest_tag_range(project: ProjectConfig) -> str:
    """git 범위 입력의 기본값: 최근 태그..HEAD (태그가 없으면 빈 문자열)."""
    try:
        tag = git_local.run_git(project.root, "describe", "--tags", "--abbrev=0").strip()
    except git_local.GitError:
        return ""
    return f"{tag}..HEAD" if tag else ""


def collect_lines(lines: Sequence[str], description: str) -> Tuple[List[FileChange], SourceInfo]:
    changes, errors = manual.parse_lines(lines)
    if errors:
        raise InputError("파일 목록 오류:\n  " + "\n  ".join(errors))
    return merge_changes([changes]), SourceInfo(kind="manual", description=description)


def collect_requests(provider: Provider, project: ProjectConfig,
                     numbers: List[int]) -> Tuple[List[FileChange], SourceInfo]:
    changes, source = provider.collect(numbers)
    check_local_has_requests(project, source, provider.label)
    return changes, source


def check_local_has_requests(project: ProjectConfig, source: SourceInfo, label: str = "MR") -> None:
    """로컬 저장소(빌드 대상)에 MR/PR 머지 커밋이 들어 있는지 확인한다."""
    if not git_local.is_git_repo(project.root):
        source.warnings.append(f"로컬 경로가 git 저장소가 아니어서 {label} 반영 여부를 확인하지 못했습니다.")
        return
    for mr in source.merge_requests:
        sha = mr.get("commit_sha")
        if not sha:
            continue
        ref = mr.get("ref") or f"!{mr['iid']}"
        has = git_local.contains_commit(project.root, sha)
        if has is False:
            source.warnings.append(
                f"로컬 HEAD 에 {label} {ref} 의 커밋({sha[:8]})이 없습니다. "
                "git pull 후 다시 빌드했는지 확인하세요."
            )
        elif has is None:
            source.warnings.append(
                f"로컬 저장소에 {label} {ref} 의 커밋({sha[:8]})이 없습니다. git fetch/pull 이 필요합니다."
            )


# ---------------------------------------------------------------------- 배포 계획
def mark_uncommitted(project: ProjectConfig, entries: List[Entry], changes: List[FileChange],
                     source: SourceInfo) -> None:
    """커밋되지 않은 로컬 수정이 있는 파일을 경고한다 (테스트용 설정 등이 운영에 섞이는 것 방지)."""
    if not git_local.is_git_repo(project.root):
        return
    try:
        dirty = git_local.dirty_paths(project.root)
    except git_local.GitError as e:
        log.warning("git status 실패: %s", e)
        return
    if project.mode == "sync":
        # 빌드 결과는 보통 git 에 없으므로, 빌드에 들어갔을 수 있는 소스의 미커밋 수정을 본다.
        prefix = f"{project.build_dir}/"
        targets = {c.path for c in changes} if changes else dirty
        hits = sorted(d for d in dirty & targets if not d.startswith(prefix))
        if hits:
            shown = ", ".join(hits[:5]) + (f" 외 {len(hits) - 5}개" if len(hits) > 5 else "")
            source.warnings.append(f"커밋되지 않은 로컬 수정이 빌드에 들어갔을 수 있습니다: {shown}")
        return
    for e in entries:
        if e.change.path in dirty and e.artifacts:
            e.warn("커밋되지 않은 로컬 수정이 있습니다 - 저장소와 다른 내용이 배포될 수 있습니다")


def plan(project: ProjectConfig, changes: List[FileChange], source: SourceInfo) -> List[Entry]:
    """배포 계획. sync 모드에서 빌드 결과가 없으면 배포할 파일이 없는 목록을 돌려준다(has_artifacts 로 확인)."""
    source.notes.extend(n for n in mapper.collect_notices(project, changes) if n not in source.notes)
    if project.mode == "sync":
        entries = sync.plan_sync(project, changes, source)
        if not has_artifacts(entries):
            return entries
    else:
        entries = mapper.plan(project, changes)
    mark_uncommitted(project, entries, changes, source)
    return entries


def has_artifacts(entries: List[Entry]) -> bool:
    return any(e.artifacts for e in entries)


def is_blocked(entries: List[Entry], source: SourceInfo, strict: bool = False) -> bool:
    return any(e.status == Status.ERROR for e in entries) or (
        strict and bool(source.warnings or any(e.status == Status.WARN for e in entries))
    )
