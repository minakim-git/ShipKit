"""GitHub REST API 연동 (Pull Request).

- 인증: Authorization: Bearer <token>. 공개 저장소는 토큰 없이도 되지만 조회 횟수 제한이 작다.
- GitHub Enterprise 는 설정의 url 을 https://<호스트>/api/v3 로 지정한다.
- 모든 요청에 timeout 과 SSL 검증(사내 CA 지정 가능)을 적용한다.
- PR 변경 파일은 페이지 처리(100개씩)하고, GitHub 한도(3000개)에 닿으면 경고한다.

목록·PR 정보는 GitLab MR 과 같은 모양의 dict 로 바꿔서 돌려준다 (iid, title, author.name, state, merged_at ...).
그래서 화면 출력과 보고서는 GitLab 과 같은 코드를 쓴다.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple, Union

import requests

from shipkit.config import GitHubConfig
from shipkit.models import ChangeType, FileChange, SourceInfo, merge_changes, normalize_repo_path

log = logging.getLogger(__name__)

PER_PAGE = 100
MAX_PR_FILES = 3000  # GitHub 가 PR 하나에 대해 돌려주는 파일 수 상한


class GitHubError(Exception):
    pass


class GitHubClient:
    def __init__(self, cfg: GitHubConfig, token: Optional[str], session: Optional[requests.Session] = None):
        self.cfg = cfg
        self.session = session or requests.Session()
        self.session.headers.update({
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"
        self.api = cfg.url.rstrip("/")
        self.anonymous = not token

    # ------------------------------------------------------------------ 저수준
    def _verify(self) -> Union[bool, str]:
        if not self.cfg.verify_ssl:
            return False
        return self.cfg.ca_bundle or True

    def _get(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> requests.Response:
        url = f"{self.api}/{endpoint.lstrip('/')}"
        log.debug("GET %s %s", url, params or "")
        try:
            resp = self.session.get(url, params=params, timeout=self.cfg.timeout, verify=self._verify())
        except requests.exceptions.SSLError as e:
            raise GitHubError(f"SSL 인증서 검증 실패: {e}\n사내 인증서라면 github.ca_bundle 에 CA 파일 경로를 지정하세요.") from e
        except requests.exceptions.Timeout as e:
            raise GitHubError(f"GitHub 응답 시간 초과({self.cfg.timeout}초): {url}") from e
        except requests.exceptions.ConnectionError as e:
            raise GitHubError(f"GitHub 에 연결할 수 없습니다 ({self.api}): {e}") from e
        if resp.status_code == 401:
            raise GitHubError("GitHub 인증 실패(401): 토큰이 틀렸거나 만료되었습니다.")
        if resp.status_code in (403, 429) and resp.headers.get("X-RateLimit-Remaining") == "0":
            hint = " 토큰을 설정하면 한도가 크게 늘어납니다." if self.anonymous else ""
            raise GitHubError(f"GitHub API 조회 한도를 다 썼습니다. 잠시 뒤 다시 시도하세요.{hint}")
        if resp.status_code == 403:
            raise GitHubError("GitHub 권한 없음(403): 토큰 권한(저장소 읽기)을 확인하세요.")
        return resp

    def _get_json(self, endpoint: str, params: Optional[Dict[str, Any]] = None, what: str = "") -> Any:
        resp = self._get(endpoint, params)
        if resp.status_code == 404:
            hint = " (비공개 저장소라면 토큰이 필요합니다)" if self.anonymous else ""
            raise GitHubError(f"GitHub 에서 찾을 수 없습니다(404): {what or endpoint}{hint}")
        if resp.status_code != 200:
            raise GitHubError(f"GitHub API 오류 {resp.status_code}: {endpoint} - {resp.text[:300]}")
        return resp.json()

    # ------------------------------------------------------------------ API
    def current_user(self) -> Dict[str, Any]:
        return self._get_json("user")

    def get_repo(self, repo: str) -> Dict[str, Any]:
        return self._get_json(f"repos/{repo}", what=f"저장소 {repo}")

    def list_pull_requests(self, repo: str, state: str = "merged", limit: int = 20,
                           search: Optional[str] = None) -> List[Dict[str, Any]]:
        """state: merged | opened | closed | all. GitHub 에는 merged 필터가 없어서 closed 에서 골라낸다."""
        api_state = {"merged": "closed", "opened": "open", "closed": "closed", "all": "all"}[state]
        per_page = PER_PAGE if (state in ("merged", "closed") or search) else min(limit, PER_PAGE)
        items = self._get_json(f"repos/{repo}/pulls", {"state": api_state, "sort": "updated", "direction": "desc",
                                                        "per_page": per_page}, what=f"저장소 {repo}")
        prs = [_normalize(pr) for pr in items]
        if state == "merged":
            prs = [pr for pr in prs if pr["state"] == "merged"]
        elif state == "closed":
            prs = [pr for pr in prs if pr["state"] == "closed"]
        if search:
            prs = [pr for pr in prs if search.lower() in pr["title"].lower()]
        return prs[:limit]

    def get_pull_request(self, repo: str, number: int) -> Dict[str, Any]:
        return _normalize(self._get_json(f"repos/{repo}/pulls/{number}", what=f"PR #{number} ({repo})"))

    def pull_request_changes(self, repo: str, number: int) -> Tuple[List[FileChange], List[str]]:
        files: List[Dict[str, Any]] = []
        page = 1
        while True:
            batch = self._get_json(f"repos/{repo}/pulls/{number}/files", {"per_page": PER_PAGE, "page": page},
                                   what=f"PR #{number} 파일 목록")
            files.extend(batch)
            if len(batch) < PER_PAGE or len(files) >= MAX_PR_FILES:
                break
            page += 1
        warnings = []
        if len(files) >= MAX_PR_FILES:
            warnings.append(f"PR #{number}: 변경 파일이 GitHub 한도({MAX_PR_FILES}개)에 닿아 누락이 있을 수 있습니다. "
                            "--git 모드로 확인하세요.")
        origin = f"#{number}"
        return [c for f in files for c in _file_to_changes(f, origin)], warnings


def _normalize(pr: Dict[str, Any]) -> Dict[str, Any]:
    merged_at = pr.get("merged_at") or ""
    state = "merged" if merged_at else ("opened" if pr.get("state") == "open" else "closed")
    return {
        "iid": pr["number"],
        "title": pr.get("title", ""),
        "author": {"name": (pr.get("user") or {}).get("login", "")},
        "state": state,
        "merged_at": merged_at,
        "updated_at": pr.get("updated_at") or "",
        "web_url": pr.get("html_url", ""),
        "merge_commit_sha": pr.get("merge_commit_sha") if merged_at else None,
        "sha": (pr.get("head") or {}).get("sha"),
        "source_branch": (pr.get("head") or {}).get("ref", ""),
        "target_branch": (pr.get("base") or {}).get("ref", ""),
    }


def _file_to_changes(f: Dict[str, Any], origin: str) -> List[FileChange]:
    status = f.get("status", "modified")
    path = normalize_repo_path(f["filename"])
    if status == "removed":
        return [FileChange(path, ChangeType.DELETED, origin)]
    if status == "added":
        return [FileChange(path, ChangeType.ADDED, origin)]
    if status == "renamed" and f.get("previous_filename"):
        return [FileChange(normalize_repo_path(f["previous_filename"]), ChangeType.DELETED, origin),
                FileChange(path, ChangeType.ADDED, origin)]
    if status == "copied":
        return [FileChange(path, ChangeType.ADDED, origin)]
    return [FileChange(path, ChangeType.MODIFIED, origin)]


def collect_from_prs(client: GitHubClient, repo: str, numbers: List[int]) -> Tuple[List[FileChange], SourceInfo]:
    """여러 PR 의 변경을 머지 시각 순서로 합친다."""
    infos = [client.get_pull_request(repo, n) for n in dict.fromkeys(numbers)]
    infos.sort(key=lambda p: (p.get("merged_at") or p.get("updated_at") or "", p["iid"]))
    source = SourceInfo(kind="github-pr", description="GitHub PR " + ", ".join(f"#{p['iid']}" for p in infos),
                        request_label="Pull Request")
    batches = []
    for pr in infos:
        if pr["state"] != "merged":
            source.warnings.append(f"PR #{pr['iid']} 는 아직 머지되지 않았습니다(상태: {pr['state']}).")
        changes, warns = client.pull_request_changes(repo, pr["iid"])
        source.warnings.extend(warns)
        batches.append(changes)
        source.merge_requests.append({
            "iid": pr["iid"],
            "ref": f"#{pr['iid']}",
            "title": pr["title"],
            "author": pr["author"]["name"],
            "state": pr["state"],
            "merged_at": pr["merged_at"],
            "source_branch": pr["source_branch"],
            "target_branch": pr["target_branch"],
            "web_url": pr["web_url"],
            "commit_sha": pr.get("merge_commit_sha") or pr.get("sha") or "",
            "file_count": len(changes),
        })
    return merge_changes(batches), source
