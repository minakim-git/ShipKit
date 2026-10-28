"""GitLab REST API v4 연동.

- 인증: PRIVATE-TOKEN 헤더 (토큰은 secrets 모듈이 환경변수/키링에서 가져온다)
- 모든 요청에 timeout 과 SSL 검증(사내 CA 지정 가능)을 적용한다.
- MR 변경 파일은 /diffs(페이지 처리, GitLab 15.7+)를 우선 쓰고, 없으면 /changes 로 대체하며
  응답이 잘린 경우(overflow) 경고를 남긴다.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple, Union
from urllib.parse import quote

import requests

from shipkit.config import GitLabConfig
from shipkit.models import ChangeType, FileChange, SourceInfo, merge_changes, normalize_repo_path

log = logging.getLogger(__name__)

MAX_PAGES = 50
PER_PAGE = 100


class GitLabError(Exception):
    pass


class GitLabClient:
    def __init__(self, cfg: GitLabConfig, token: str, session: Optional[requests.Session] = None):
        self.cfg = cfg
        self.session = session or requests.Session()
        self.session.headers.update({"PRIVATE-TOKEN": token})
        base = cfg.url.rstrip("/")
        self.api = base if base.endswith("/api/v4") else base + "/api/v4"

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
            raise GitLabError(
                f"SSL 인증서 검증 실패: {e}\n사내 인증서라면 gitlab.ca_bundle 에 CA 파일 경로를 지정하세요."
            ) from e
        except requests.exceptions.Timeout as e:
            raise GitLabError(f"GitLab 응답 시간 초과({self.cfg.timeout}초): {url}") from e
        except requests.exceptions.ConnectionError as e:
            raise GitLabError(f"GitLab 서버에 연결할 수 없습니다 ({self.cfg.url}): {e}") from e
        if resp.status_code == 401:
            raise GitLabError("GitLab 인증 실패(401): 토큰이 없거나 만료되었습니다.")
        if resp.status_code == 403:
            raise GitLabError("GitLab 권한 없음(403): 토큰 권한(read_api)과 프로젝트 접근 권한을 확인하세요.")
        return resp

    def _get_json(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> Any:
        resp = self._get(endpoint, params)
        if resp.status_code == 404:
            raise GitLabError(f"GitLab 리소스를 찾을 수 없습니다(404): {endpoint}")
        if resp.status_code != 200:
            raise GitLabError(f"GitLab API 오류 {resp.status_code}: {endpoint} - {resp.text[:300]}")
        return resp.json()

    def _get_paged(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> Optional[List[Any]]:
        """모든 페이지를 합쳐 반환. 엔드포인트가 없으면(404) None."""
        items: List[Any] = []
        page = 1
        while True:
            p = dict(params or {}, page=page, per_page=PER_PAGE)
            resp = self._get(endpoint, p)
            if resp.status_code == 404 and page == 1:
                return None
            if resp.status_code != 200:
                raise GitLabError(f"GitLab API 오류 {resp.status_code}: {endpoint} - {resp.text[:300]}")
            items.extend(resp.json())
            next_page = resp.headers.get("X-Next-Page", "")
            if not next_page:
                return items
            page = int(next_page)
            if page > MAX_PAGES:
                raise GitLabError(f"페이지가 너무 많습니다(>{MAX_PAGES}): {endpoint}")

    @staticmethod
    def _pid(project: Union[str, int]) -> str:
        return quote(str(project), safe="")

    # ------------------------------------------------------------------ API
    def current_user(self) -> Dict[str, Any]:
        return self._get_json("user")

    def get_project(self, project: Union[str, int]) -> Dict[str, Any]:
        return self._get_json(f"projects/{self._pid(project)}")

    def list_merge_requests(self, project: Union[str, int], state: str = "merged",
                            limit: int = 20, search: Optional[str] = None) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {"order_by": "updated_at", "sort": "desc", "per_page": limit}
        if state != "all":
            params["state"] = state
        if search:
            params["search"] = search
        return self._get_json(f"projects/{self._pid(project)}/merge_requests", params)

    def get_merge_request(self, project: Union[str, int], iid: int) -> Dict[str, Any]:
        try:
            return self._get_json(f"projects/{self._pid(project)}/merge_requests/{iid}")
        except GitLabError as e:
            if "404" in str(e):
                raise GitLabError(f"MR !{iid} 를 찾을 수 없습니다 (프로젝트: {project}).") from e
            raise

    def merge_request_changes(self, project: Union[str, int], iid: int) -> Tuple[List[FileChange], List[str]]:
        """MR 의 변경 파일 목록과 경고 목록을 반환한다."""
        warnings: List[str] = []
        base = f"projects/{self._pid(project)}/merge_requests/{iid}"
        diffs = self._get_paged(f"{base}/diffs")
        if diffs is None:
            log.debug("/diffs 미지원 GitLab, /changes 로 대체")
            data = self._get_json(f"{base}/changes")
            diffs = data.get("changes", [])
            if data.get("overflow") or str(data.get("changes_count", "")).endswith("+"):
                warnings.append(
                    f"MR !{iid}: 변경 파일이 많아 GitLab 응답이 잘렸습니다(overflow). "
                    "누락 파일이 있을 수 있으니 --git 모드로 확인하세요."
                )
        origin = f"!{iid}"
        return [c for d in diffs for c in _diff_to_changes(d, origin)], warnings


def _diff_to_changes(d: Dict[str, Any], origin: str) -> List[FileChange]:
    old, new = d.get("old_path") or "", d.get("new_path") or ""
    if d.get("deleted_file"):
        return [FileChange(normalize_repo_path(old), ChangeType.DELETED, origin)]
    if d.get("new_file"):
        return [FileChange(normalize_repo_path(new), ChangeType.ADDED, origin)]
    if d.get("renamed_file") and old != new:
        return [
            FileChange(normalize_repo_path(old), ChangeType.DELETED, origin),
            FileChange(normalize_repo_path(new), ChangeType.ADDED, origin),
        ]
    return [FileChange(normalize_repo_path(new), ChangeType.MODIFIED, origin)]


def collect_from_mrs(client: GitLabClient, project: Union[str, int],
                     iids: List[int]) -> Tuple[List[FileChange], SourceInfo]:
    """여러 MR 의 변경을 머지 시각 순서로 합친다."""
    infos = [client.get_merge_request(project, iid) for iid in dict.fromkeys(iids)]
    infos.sort(key=lambda m: (m.get("merged_at") or m.get("updated_at") or "", m["iid"]))

    source = SourceInfo(kind="gitlab-mr", description="GitLab MR " + ", ".join(f"!{m['iid']}" for m in infos))
    batches = []
    for mr in infos:
        if mr.get("state") != "merged":
            source.warnings.append(f"MR !{mr['iid']} 는 아직 머지되지 않았습니다(상태: {mr.get('state')}).")
        changes, warns = client.merge_request_changes(project, mr["iid"])
        source.warnings.extend(warns)
        batches.append(changes)
        source.merge_requests.append({
            "iid": mr["iid"],
            "title": mr.get("title", ""),
            "author": (mr.get("author") or {}).get("name", ""),
            "state": mr.get("state", ""),
            "merged_at": mr.get("merged_at") or "",
            "source_branch": mr.get("source_branch", ""),
            "target_branch": mr.get("target_branch", ""),
            "web_url": mr.get("web_url", ""),
            "commit_sha": mr.get("merge_commit_sha") or mr.get("squash_commit_sha") or mr.get("sha") or "",
            "file_count": len(changes),
        })
    return merge_changes(batches), source
