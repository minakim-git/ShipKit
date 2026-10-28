"""변경 출처 공통 틀: GitLab MR 과 GitHub PR 을 같은 방식(목록 조회, 변경 수집)으로 다룬다.

CLI·대화형 모드는 Provider 만 보고 동작하므로, 새 서비스(Gitea 등)를 붙일 때는
클라이언트와 make_provider 의 분기만 추가하면 된다.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from shipkit import CLI_NAME
from shipkit.config import Config, ProjectConfig
from shipkit.models import FileChange, SourceInfo
from shipkit.secrets import get_token
from shipkit.sources.github import GitHubClient, collect_from_prs
from shipkit.sources.gitlab import GitLabClient, collect_from_mrs

KINDS = ("gitlab", "github")


class ProviderError(Exception):
    pass


@dataclass
class Provider:
    kind: str        # "gitlab" | "github"
    service: str     # "GitLab" | "GitHub"
    label: str       # "MR" | "PR"
    prefix: str      # 번호 앞 기호 "!" | "#"
    ref: str         # GitLab 프로젝트 ID / GitHub owner/name
    client: Any
    _list: Callable[..., List[Dict[str, Any]]]
    _collect: Callable[[Any, str, List[int]], Tuple[List[FileChange], SourceInfo]]

    def list(self, state: str = "merged", limit: int = 20, search: Optional[str] = None) -> List[Dict[str, Any]]:
        return self._list(self.ref, state=state, limit=limit, search=search)

    def collect(self, numbers: List[int]) -> Tuple[List[FileChange], SourceInfo]:
        return self._collect(self.client, self.ref, numbers)


def gitlab_client(cfg: Config) -> GitLabClient:
    return GitLabClient(cfg.gitlab, get_token(cfg.gitlab))


def github_client(cfg: Config) -> GitHubClient:
    return GitHubClient(cfg.github, get_token(cfg.github, required=False))


def available(cfg: Config, project: ProjectConfig) -> List[str]:
    kinds = []
    if cfg.gitlab and project.gitlab_project:
        kinds.append("gitlab")
    if cfg.github and project.github_repo:
        kinds.append("github")
    return kinds


def make_provider(cfg: Config, project: ProjectConfig, kind: str) -> Provider:
    if kind == "gitlab":
        if cfg.gitlab is None:
            raise ProviderError("개인 설정에 gitlab 섹션이 없습니다.")
        if not project.gitlab_project:
            hint = f" 이 프로젝트는 GitHub 저장소입니다: '{CLI_NAME} pr' / --pr 을 쓰세요." if project.github_repo else ""
            raise ProviderError(f"projects.{project.name}.gitlab_project 가 설정되지 않았습니다.{hint}")
        client = gitlab_client(cfg)
        return Provider("gitlab", "GitLab", "MR", "!", project.gitlab_project, client,
                        client.list_merge_requests, collect_from_mrs)
    if kind == "github":
        if cfg.github is None:
            raise ProviderError("개인 설정에 github 섹션이 없습니다. (github.com 이면 'github: {}' 만 적어도 됩니다)")
        if not project.github_repo:
            hint = f" 이 프로젝트는 GitLab 저장소입니다: '{CLI_NAME} mr' / --mr 을 쓰세요." if project.gitlab_project else ""
            raise ProviderError(f"projects.{project.name}.github_repo (owner/name) 가 설정되지 않았습니다.{hint}")
        client = github_client(cfg)
        return Provider("github", "GitHub", "PR", "#", project.github_repo, client,
                        client.list_pull_requests, collect_from_prs)
    raise ProviderError(f"알 수 없는 변경 출처입니다: {kind}")
