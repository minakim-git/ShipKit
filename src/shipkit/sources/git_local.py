"""로컬 git 저장소에서 두 리비전 사이의 변경 파일을 수집한다 (GitLab 토큰 불필요)."""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Set, Tuple

from shipkit.models import ChangeType, FileChange, normalize_repo_path


class GitError(Exception):
    pass


# 화면 프로그램(ShipKit.exe)에서 git 을 부를 때 명령 창이 깜빡이지 않게 한다 (Windows 전용 옵션).
# 입력도 막아 두어 git 이 무언가를 물어보며 멈추지 않게 한다.
_QUIET = {"stdin": subprocess.DEVNULL,
          **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {})}


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
            capture_output=True, timeout=60, **_QUIET,
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


def contains_commit(root: Path, sha: str, ref: str = "HEAD") -> Optional[bool]:
    """ref(기본 HEAD)가 sha 를 포함하면 True, 아니면 False, 로컬에 없는 커밋(또는 ref)이면 None."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "merge-base", "--is-ancestor", sha, ref],
            capture_output=True, timeout=60, **_QUIET,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if proc.returncode == 0:
        return True
    if proc.returncode == 1:
        return False
    return None


def current_branch(root: Path) -> Optional[str]:
    """지금 체크아웃한 브랜치 이름 (detached HEAD 이거나 확인하지 못하면 None)."""
    try:
        branch = run_git(root, "rev-parse", "--abbrev-ref", "HEAD").strip()
    except GitError:
        return None
    return None if branch in ("", "HEAD") else branch


def remote_contains(root: Path, sha: str, branch: str) -> bool:
    """원격 추적 브랜치(origin/<branch> 등) 가운데 하나라도 sha 를 포함하는지 (마지막으로 받아온 원격 기준)."""
    try:
        refs = run_git(root, "for-each-ref", "--format=%(refname)", f"refs/remotes/*/{branch}").split()
    except GitError:
        return False
    return any(contains_commit(root, sha, ref) for ref in refs)


def remote_path(root: Path, host_url: str, remote: str = "origin") -> Optional[str]:
    """원격 저장소 주소가 host_url 의 서버를 가리키면 그 안의 경로("그룹/프로젝트")를 돌려준다.

    https://gitlab.acme.local/erp/web.git, git@gitlab.acme.local:erp/web.git -> erp/web
    """
    try:
        url = run_git(root, "remote", "get-url", remote).strip()
    except GitError:
        return None
    host = re.sub(r"^[a-z]+://", "", host_url.strip().lower()).split("/")[0].split("@")[-1].split(":")[0]
    m = re.match(r"^(?:[a-z+]+://)?(?:[^@/]+@)?([^/:]+)(?::\d+)?[:/](.+?)(?:\.git)?/?$", url, re.IGNORECASE)
    if not host or not m or m.group(1).lower() != host:
        return None
    return m.group(2)


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


@dataclass
class BranchStatus:
    branch: Optional[str]          # None = 브랜치가 아닌 커밋에 있음 (detached HEAD)
    upstream: Optional[str]        # 추적하는 원격 브랜치 (예: origin/main)
    ahead: int = 0                 # 원격에 아직 안 올린 로컬 커밋 수
    behind: int = 0                # 로컬에 아직 안 받은 원격 커밋 수
    fetched: Optional[bool] = None  # 원격 최신 상태를 받아왔는지 (None = 시도 안 함)


def fetch(root: Path, timeout: int = 20) -> bool:
    """원격 브랜치 정보만 받아온다 (작업 파일은 건드리지 않음). 로그인 창이 뜨지 않게 막고, 실패하면 False."""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never", GIT_ASKPASS="echo")
    try:
        proc = subprocess.run(["git", "-C", str(root), "fetch", "--quiet", "--no-tags"],
                              capture_output=True, timeout=timeout, env=env, **_QUIET)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def pull_ff(root: Path, timeout: int = 120) -> int:
    """원격 최신 커밋을 fast-forward 로만 받는다 (merge·rebase 는 하지 않음). 반환: 받은 커밋 수.

    로컬과 원격이 갈라졌거나, 커밋 안 한 수정이 받을 파일과 겹치면 아무것도 바꾸지 않고 GitError.
    """
    before = run_git(root, "rev-parse", "HEAD").strip()
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never", GIT_ASKPASS="echo")
    try:
        proc = subprocess.run(["git", "-C", str(root), "pull", "--ff-only", "--no-rebase", "--quiet"],
                              capture_output=True, timeout=timeout, env=env, **_QUIET)
    except FileNotFoundError as e:
        raise GitError("git 실행 파일을 찾을 수 없습니다. Git 이 설치되어 PATH 에 있는지 확인하세요.") from e
    except subprocess.TimeoutExpired as e:
        raise GitError(f"git pull 이 {timeout}초 안에 끝나지 않았습니다. 네트워크를 확인하세요.") from e
    if proc.returncode != 0:
        msg = proc.stderr.decode("utf-8", "replace").strip()
        low = msg.lower()
        if "fast-forward" in low or "diverg" in low:
            raise GitError("로컬에 push 하지 않은 커밋이 있어 원격과 갈라졌습니다. 자동으로 합치지 않으니 "
                           "직접 pull(merge 또는 rebase) 하세요.")
        if "would be overwritten" in low or "untracked working tree files" in low:
            files = [line.strip() for line in msg.splitlines() if line.startswith(("\t", "    "))]
            raise GitError("커밋하지 않은 수정이 받을 파일과 겹쳐서 받지 않았습니다. 그 파일을 커밋하거나 되돌린 뒤 "
                           "다시 하세요." + (f"\n겹치는 파일: {', '.join(files[:5])}" if files else ""))
        if "authentication" in low or "could not read" in low or "permission denied" in low:
            raise GitError("원격 저장소 로그인에 실패했습니다. 명령 창에서 git pull 을 한 번 해서 로그인 정보를 저장하세요.\n" + msg)
        raise GitError(f"git pull 실패: {msg}")
    after = run_git(root, "rev-parse", "HEAD").strip()
    return 0 if before == after else int(run_git(root, "rev-list", "--count", f"{before}..{after}").strip() or 0)


def branch_status(root: Path, do_fetch: bool = False) -> BranchStatus:
    branch = run_git(root, "rev-parse", "--abbrev-ref", "HEAD").strip()
    branch = None if branch == "HEAD" else branch
    try:
        upstream = run_git(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}").strip() or None
    except GitError:
        upstream = None
    status = BranchStatus(branch, upstream)
    if upstream is None:
        return status
    if do_fetch:
        status.fetched = fetch(root)
    counts = run_git(root, "rev-list", "--left-right", "--count", "HEAD...@{u}").split()
    if len(counts) == 2:
        status.ahead, status.behind = int(counts[0]), int(counts[1])
    return status


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
