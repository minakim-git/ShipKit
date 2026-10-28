"""로컬 git 저장소에서 두 리비전 사이의 변경 파일을 수집한다 (GitLab 토큰 불필요)."""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List, Optional, Set, Tuple

from shipkit.models import ChangeType, FileChange, normalize_repo_path


class GitError(Exception):
    pass


def parse_range(rev_range: str) -> Tuple[str, str]:
    """'A..B' -> (A, B), 'A' -> (A, HEAD). 'A...B' 는 merge-base 기준이라 지원하지 않는다."""
    if "..." in rev_range:
        raise GitError("'A...B' 형식은 지원하지 않습니다. 'A..B' 를 사용하세요.")
    if ".." in rev_range:
        base, head = rev_range.split("..", 1)
        if not base or not head:
            raise GitError(f"리비전 범위 형식이 잘못되었습니다: {rev_range}")
        return base, head
    return rev_range, "HEAD"


def run_git(root: Path, *args: str) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "-c", "core.quotepath=false", *args],
            capture_output=True, timeout=60,
        )
    except FileNotFoundError as e:
        raise GitError("git 실행 파일을 찾을 수 없습니다. Git 이 설치되어 PATH 에 있는지 확인하세요.") from e
    except subprocess.TimeoutExpired as e:
        raise GitError("git 명령이 60초 안에 끝나지 않았습니다.") from e
    if proc.returncode != 0:
        msg = proc.stderr.decode("utf-8", "replace").strip()
        raise GitError(f"git {' '.join(args)} 실패: {msg}")
    return proc.stdout.decode("utf-8", "replace")


def is_git_repo(root: Path) -> bool:
    try:
        return run_git(root, "rev-parse", "--is-inside-work-tree").strip() == "true"
    except GitError:
        return False


def contains_commit(root: Path, sha: str) -> Optional[bool]:
    """HEAD 가 sha 를 포함하면 True, 아니면 False, 로컬에 없는 커밋이면 None."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "merge-base", "--is-ancestor", sha, "HEAD"],
            capture_output=True, timeout=60,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if proc.returncode == 0:
        return True
    if proc.returncode == 1:
        return False
    return None


def resolve_commit(root: Path, rev: str) -> Optional[str]:
    try:
        return run_git(root, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}").strip() or None
    except GitError:
        return None


def dirty_paths(root: Path) -> Set[str]:
    """커밋되지 않은 수정(스테이징 포함)이 있는 추적 파일 경로. 추적 안 되는 새 파일도 포함."""
    out = run_git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    paths: Set[str] = set()
    tokens = out.split("\0")
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if len(tok) < 4:
            i += 1
            continue
        paths.add(tok[3:])
        i += 2 if tok[0] in "RC" else 1  # 이름변경은 원래 경로가 다음 토큰에 온다
    return paths


def collect_from_git(root: Path, rev_range: str) -> Tuple[List[FileChange], str]:
    base, head = parse_range(rev_range)
    out = run_git(root, "diff", "--name-status", "-M", "--no-color", "-z", base, head)
    changes = parse_name_status_z(out, origin="git")
    desc = f"git diff {base}..{head}"
    return changes, desc


def parse_name_status_z(out: str, origin: str) -> List[FileChange]:
    tokens = out.split("\0")
    changes: List[FileChange] = []
    i = 0
    while i < len(tokens):
        status = tokens[i]
        if not status:
            i += 1
            continue
        code = status[0]
        if code in "RC":
            old, new = tokens[i + 1], tokens[i + 2]
            if code == "R":
                changes.append(FileChange(normalize_repo_path(old), ChangeType.DELETED, origin))
            changes.append(FileChange(normalize_repo_path(new), ChangeType.ADDED, origin))
            i += 3
            continue
        path = normalize_repo_path(tokens[i + 1])
        change = {"A": ChangeType.ADDED, "D": ChangeType.DELETED}.get(code, ChangeType.MODIFIED)
        changes.append(FileChange(path, change, origin))
        i += 2
    return changes
