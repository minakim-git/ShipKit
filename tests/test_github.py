"""4단계: GitHub PR 수집, 변경 출처 공통 틀(provider), CLI·대화형 모드 연결."""
import pytest
import yaml

from shipkit import cli
from shipkit.config import ConfigError, GitHubConfig, parse_config
from shipkit.interactive import Wizard
from shipkit.models import ChangeType as C
from shipkit.sources import providers
from shipkit.sources.github import GitHubClient, GitHubError, collect_from_prs

from conftest import FakeResponse, FakeSession, write

API = "https://api.github.com"
REPO = "me/web"


def _pr(number, merged_at=None, state="closed", title=None):
    return {
        "number": number, "title": title or f"PR {number}", "user": {"login": "mina"}, "state": state,
        "merged_at": merged_at, "updated_at": "2026-10-20T00:00:00Z", "html_url": f"https://github.com/{REPO}/pull/{number}",
        "merge_commit_sha": f"sha{number}", "head": {"ref": f"feature/{number}", "sha": f"head{number}"},
        "base": {"ref": "main"},
    }


def _client(routes, token="tok", **cfg):
    session = FakeSession(routes)
    return GitHubClient(GitHubConfig(**cfg), token, session), session


# ---------------------------------------------------------------------- 설정
def test_github_config_defaults_and_repo_validation():
    cfg = parse_config({"github": {}, "projects": {"a": {
        "root": "D:/w/a", "output_dir": "D:/o", "server_path": "/srv/a", "github_repo": "me/web"}}})
    assert cfg.github.url == API and cfg.github.token_env == "GITHUB_TOKEN"
    assert cfg.project("a").github_repo == "me/web"
    with pytest.raises(ConfigError, match="owner/name"):
        parse_config({"projects": {"a": {"root": "x", "output_dir": "y", "server_path": "/s", "github_repo": "web"}}})
    with pytest.raises(ConfigError, match="평문"):
        parse_config({"github": {"token": "ghp_x"}, "projects": {}})
    ghe = parse_config({"github": {"url": "https://ghe.acme.local/api/v3/"}, "projects": {}}).github
    assert ghe.url == "https://ghe.acme.local/api/v3"


# ---------------------------------------------------------------------- 클라이언트
def test_list_pull_requests_states_and_search():
    routes = {f"{API}/repos/{REPO}/pulls": FakeResponse(200, [
        _pr(5, merged_at="2026-10-02T00:00:00Z", title="Fix login"),
        _pr(4, merged_at=None, title="Abandoned"),
        _pr(3, merged_at="2026-10-01T00:00:00Z", title="Add page"),
    ])}
    client, session = _client(routes)
    merged = client.list_pull_requests(REPO, state="merged", limit=10)
    assert [p["iid"] for p in merged] == [5, 3] and merged[0]["author"]["name"] == "mina"
    assert session.calls[-1][1]["state"] == "closed"
    assert [p["iid"] for p in client.list_pull_requests(REPO, state="closed")] == [4]
    assert [p["iid"] for p in client.list_pull_requests(REPO, state="merged", search="LOGIN")] == [5]
    assert session.headers["Authorization"] == "Bearer tok"
    assert all(call[2] == 15.0 and call[3] is True for call in session.calls)


def test_collect_from_prs_paging_renames_and_order():
    files_page1 = [{"filename": f"src/f{i}.js", "status": "modified"} for i in range(100)]
    files_page2 = [
        {"filename": "src/new.js", "status": "renamed", "previous_filename": "src/old.js"},
        {"filename": "src/gone.js", "status": "removed"},
    ]
    routes = {
        f"{API}/repos/{REPO}/pulls/8": FakeResponse(200, _pr(8, merged_at="2026-10-03T00:00:00Z")),
        f"{API}/repos/{REPO}/pulls/7": FakeResponse(200, _pr(7, merged_at="2026-10-01T00:00:00Z")),
        f"{API}/repos/{REPO}/pulls/8/files": [FakeResponse(200, files_page1), FakeResponse(200, files_page2)],
        f"{API}/repos/{REPO}/pulls/7/files": [FakeResponse(200, [{"filename": "src/gone.js", "status": "added"}])],
    }
    client, _ = _client(routes)
    changes, source = collect_from_prs(client, REPO, [8, 7])
    by_path = {c.path: c.change for c in changes}
    assert len(changes) == 102
    assert by_path["src/old.js"] == C.DELETED and by_path["src/new.js"] == C.ADDED
    assert "src/gone.js" not in by_path            # #7 에서 추가 -> #8 에서 삭제 = 상쇄
    assert [m["ref"] for m in source.merge_requests] == ["#7", "#8"]
    assert source.request_label == "Pull Request" and source.merge_requests[0]["commit_sha"] == "sha7"


def test_github_errors():
    client, _ = _client({f"{API}/user": FakeResponse(401, {})})
    with pytest.raises(GitHubError, match="401"):
        client.current_user()
    client, _ = _client({}, token=None)
    with pytest.raises(GitHubError, match="비공개 저장소라면 토큰"):
        client.get_repo(REPO)
    limited = FakeResponse(403, {}, {"X-RateLimit-Remaining": "0"})
    client, _ = _client({f"{API}/repos/{REPO}": limited}, token=None)
    with pytest.raises(GitHubError, match="한도.*토큰"):
        client.get_repo(REPO)


# ---------------------------------------------------------------------- provider / CLI
def _write_cfg(tmp_path, maven_project, **project):
    data = {
        "gitlab": {"url": "https://gitlab.acme.local"},
        "github": {},
        "defaults": {"output_dir": (tmp_path / "out").as_posix()},
        "projects": {"erp": dict({"root": maven_project.as_posix(), "server_path": "/app/ROOT"}, **project)},
    }
    path = tmp_path / "shipkit.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


@pytest.fixture
def fake_github(monkeypatch):
    routes = {
        f"{API}/repos/{REPO}/pulls": FakeResponse(200, [_pr(12, merged_at="2026-10-05T00:00:00Z", title="JSP 수정")]),
        f"{API}/repos/{REPO}/pulls/12": FakeResponse(200, _pr(12, merged_at="2026-10-05T00:00:00Z", title="JSP 수정")),
        f"{API}/repos/{REPO}/pulls/12/files": [FakeResponse(200, [
            {"filename": "src/main/webapp/WEB-INF/views/user/list.jsp", "status": "modified"}])],
    }
    monkeypatch.setattr(providers, "github_client", lambda cfg: _client(routes)[0])
    return routes


def test_make_provider_guidance(tmp_path, maven_project):
    cfg = parse_config(yaml.safe_load(_write_cfg(tmp_path, maven_project, github_repo=REPO).read_text()))
    project = cfg.project("erp")
    assert providers.available(cfg, project) == ["github"]
    with pytest.raises(providers.ProviderError, match="GitHub 저장소입니다"):
        providers.make_provider(cfg, project, "gitlab")


def test_cli_pr_list_and_package(tmp_path, maven_project, fake_github, capsys):
    cfg = _write_cfg(tmp_path, maven_project, github_repo=REPO)
    assert cli.main(["pr", "-c", str(cfg)]) == 0
    assert "#12" in capsys.readouterr().out
    assert cli.main(["package", "-c", str(cfg), "--pr", "12"]) == 0, capsys.readouterr().out
    out = capsys.readouterr().out
    assert "GitHub PR #12" in out and "로컬 경로가 git 저장소가 아니어서 PR" in out
    pkg = next((tmp_path / "out").iterdir())
    report = (pkg / "report.md").read_text(encoding="utf-8")
    assert "## Pull Request" in report and f"[#12](https://github.com/{REPO}/pull/12)" in report
    assert cli.main(["mr", "-c", str(cfg)]) == 2  # GitLab 프로젝트가 아님


def test_cli_rejects_mr_and_pr_together(tmp_path, maven_project):
    cfg = _write_cfg(tmp_path, maven_project, github_repo=REPO)
    assert cli.main(["package", "-c", str(cfg), "--pr", "1", "--mr", "2"]) == 2


def test_wizard_pr_flow(tmp_path, maven_project, fake_github):
    cfg = parse_config(yaml.safe_load(_write_cfg(tmp_path, maven_project, github_repo=REPO).read_text()))
    answers = iter(["1", "#12", "", ""])           # 출처: GitHub PR / #12 / 만들기 Y / 압축 N
    wizard = Wizard(cfg, ask=lambda prompt: next(answers), out=lambda *a: None, cwd=maven_project)
    assert wizard.run() == 0
    pkg = next((tmp_path / "out").iterdir())
    assert (pkg / "manifest.txt").read_text(encoding="utf-8") == "WEB-INF/views/user/list.jsp\n"


def test_token_command_targets(tmp_path, maven_project, capsys, monkeypatch):
    cfg = _write_cfg(tmp_path, maven_project)
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    monkeypatch.setattr("shipkit.secrets._keyring", lambda: None)
    assert cli.main(["token", "status", "-c", str(cfg)]) == 1  # GitLab 토큰 없음
    out = capsys.readouterr().out
    assert "GitHub https://api.github.com: 환경변수 GITHUB_TOKEN" in out and "GitLab" in out
    assert cli.main(["token", "status", "github", "-c", str(cfg)]) == 0
    assert cli.main(["token", "set", "-c", str(cfg)]) == 2      # 대상이 둘이면 지정해야 함
