"""배포 시작 전 점검: 패키지를 만들기 전에 로컬 저장소·빌드·연결 상태의 문제를 미리 알려준다.

결과는 화면에 짧은 배지로 보여 줄 수 있게 (tone, 글, 도움말) 목록으로 돌려준다.
tone: success(정상) / info(참고) / warning(확인 필요) / danger(이대로는 실패)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from shipkit.config import Config, ProjectConfig
from shipkit.secrets import token_source
from shipkit.sources import git_local, providers
from shipkit.sync import build_root

SERVICE_NAME = {"gitlab": "GitLab", "github": "GitHub"}


@dataclass
class Check:
    tone: str
    text: str
    hint: str = ""


def run(cfg: Optional[Config], project: ProjectConfig, fetch: bool = True) -> List[Check]:
    if not project.root.is_dir():
        return [Check("danger", "로컬 경로가 없습니다", f"{project.root} 폴더를 찾을 수 없습니다. 프로젝트 설정을 확인하세요.")]
    checks: List[Check] = []
    checks += _git_checks(project, fetch)
    checks += _build_checks(project)
    if cfg is not None:
        checks += _token_checks(cfg, project)
    return checks


def _git_checks(project: ProjectConfig, fetch: bool) -> List[Check]:
    if not git_local.is_git_repo(project.root):
        return [Check("info", "git 저장소 아님", "git 범위·MR 반영 확인을 쓸 수 없습니다.")]
    try:
        st = git_local.branch_status(project.root, do_fetch=fetch)
        dirty = len(git_local.dirty_paths(project.root))
    except git_local.GitError as e:
        return [Check("warning", "git 상태 확인 실패", str(e))]
    checks = [Check("neutral", f"브랜치 {st.branch}" if st.branch else "브랜치 아님 (detached HEAD)",
                    f"추적: {st.upstream}" if st.upstream else "추적하는 원격 브랜치가 없습니다.")]
    basis = "" if st.fetched else " (마지막으로 받아온 원격 기준)"
    if st.behind:
        checks.append(Check("warning", f"원격보다 {st.behind}커밋 뒤처짐",
                            f"git pull 후 다시 빌드하세요. 지금 만들면 최신 변경이 빠집니다{basis}."))
    elif st.upstream:
        checks.append(Check("success", "원격과 같음" if not st.ahead else f"원격보다 {st.ahead}커밋 앞섬",
                            "로컬 저장소가 원격 최신 내용을 갖고 있습니다" + basis + "." if not st.ahead else
                            "아직 push 하지 않은 로컬 커밋이 있습니다. 배포 대상에 들어가는지 확인하세요."))
    if st.upstream and st.fetched is False:
        checks.append(Check("neutral", "원격 확인 못 함", "네트워크나 로그인 문제로 원격 최신 상태를 받아오지 못했습니다."))
    if dirty:
        checks.append(Check("warning", f"커밋 안 된 수정 {dirty}개",
                            "저장소와 다른 내용이 배포될 수 있습니다. 미리보기에서 해당 파일에 경고가 붙습니다."))
    return checks


def _build_checks(project: ProjectConfig) -> List[Check]:
    if project.mode == "sync":
        root = build_root(project)
        if not root.is_dir() or not any(f.is_file() for f in root.rglob("*")):
            return [Check("danger", "빌드 결과 없음", f"{root} 가 비어 있습니다. 먼저 빌드하세요.")]
        return []
    missing = []
    for module in project.modules:
        base = project.root / module if module else project.root
        for rule in project.rules:
            if rule.build_dir and not (base / rule.build_dir).is_dir():
                missing.append((base / rule.build_dir))
    if missing:
        return [Check("warning", "빌드 결과 폴더 없음",
                      f"{missing[0]} 가 없습니다. .java 를 배포하려면 먼저 빌드(컴파일)하세요.")]
    return []


def _token_checks(cfg: Config, project: ProjectConfig) -> List[Check]:
    checks = []
    for kind in providers.available(cfg, project):
        service = getattr(cfg, kind)
        try:
            has = token_source(service) is not None
        except Exception:  # 키링을 읽지 못해도 점검 때문에 화면이 멈추지 않게
            has = False
        if not has and kind == "gitlab":
            checks.append(Check("danger", "GitLab 토큰 없음", "왼쪽 [연결] 메뉴에서 토큰을 저장해야 MR 목록을 불러올 수 있습니다."))
        elif not has:
            checks.append(Check("info", "GitHub 토큰 없음", "공개 저장소만 조회할 수 있습니다."))
    return checks


def worst(checks: List[Check]) -> str:
    """가장 심각한 tone (요약 표시용)."""
    for tone in ("danger", "warning", "info"):
        if any(c.tone == tone for c in checks):
            return tone
    return "success"
