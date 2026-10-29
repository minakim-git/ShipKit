"""시작 전 점검: 원격보다 뒤처짐·미커밋 수정·빌드 결과·토큰을 배포 전에 알려준다."""
import shutil
import subprocess

import pytest

from shipkit import preflight
from shipkit.config import parse_config
from shipkit.sources import git_local

from conftest import make_config, write

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git 이 없습니다")


def git(root, *args):
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                   check=True, capture_output=True)


def texts(checks):
    return {c.text: c.tone for c in checks}


def test_behind_remote_and_dirty(maven_project, tmp_path):
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    git(maven_project, "init", "-q", "-b", "main")
    git(maven_project, "add", "-A")
    git(maven_project, "commit", "-q", "-m", "init")
    git(maven_project, "remote", "add", "origin", str(remote))
    git(maven_project, "push", "-q", "-u", "origin", "main")

    other = tmp_path / "other"                                  # 다른 사람이 원격에 커밋을 올린 상황
    git(tmp_path, "clone", "-q", str(remote), str(other))
    write(other / "pom.xml", "<project>v2</project>")
    git(other, "commit", "-q", "-am", "teammate change")
    git(other, "push", "-q")
    write(maven_project / "src/main/webapp/WEB-INF/views/user/list.jsp", "local edit")

    project = make_config(maven_project, tmp_path / "out").projects["erp"]
    result = texts(preflight.run(None, project))
    assert result["브랜치 main"] == "neutral"
    assert result["원격보다 1커밋 뒤처짐"] == "warning"        # fetch 로 받아온 원격 기준
    assert result["커밋 안 된 수정 1개"] == "warning"
    assert preflight.worst(preflight.run(None, project, fetch=False)) == "warning"

    git(maven_project, "pull", "-q")
    git(maven_project, "checkout", "-q", "--", ".")
    assert texts(preflight.run(None, project))["원격과 같음"] == "success"


def test_build_and_token_checks(tmp_path, monkeypatch):
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    monkeypatch.setattr("shipkit.preflight.token_source", lambda cfg: None)
    web = tmp_path / "web"
    web.mkdir()
    cfg = parse_config({"gitlab": {"url": "https://gitlab.acme.local"}, "projects": {"web": {
        "root": str(web), "output_dir": str(tmp_path / "out"), "server_path": "/var/www", "preset": "vite",
        "gitlab_project": "12"}}})
    result = texts(preflight.run(cfg, cfg.projects["web"]))
    assert result["git 저장소 아님"] == "info"
    assert result["빌드 결과 없음"] == "danger"
    assert result["GitLab 토큰 없음"] == "danger"

    missing = make_config(tmp_path / "nope", tmp_path / "out").projects["erp"]
    assert [c.text for c in preflight.run(None, missing)] == ["로컬 경로가 없습니다"]


def test_git_calls_never_open_a_console_window(tmp_path, monkeypatch):
    """화면 프로그램에서 git 을 부를 때 명령 창이 깜빡이면 안 된다 (Windows: CREATE_NO_WINDOW)."""
    import os
    from shipkit.sources import git_local
    seen = []
    real = subprocess.run

    def spy(*args, **kwargs):
        seen.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(git_local.subprocess, "run", spy)
    git_local.is_git_repo(tmp_path)
    git_local.contains_commit(tmp_path, "HEAD")
    git_local.fetch(tmp_path)
    assert len(seen) == 3
    for kwargs in seen:
        assert kwargs["stdin"] == subprocess.DEVNULL
        if os.name == "nt":
            assert kwargs["creationflags"] & subprocess.CREATE_NO_WINDOW


@pytest.mark.parametrize("url,expected", [
    ("https://gitlab.acme.local/erp/web.git", "erp/web"),
    ("git@gitlab.acme.local:erp/web.git", "erp/web"),
    ("ssh://git@gitlab.acme.local:2222/grp/sub/app.git", "grp/sub/app"),
    ("https://github.com/acme/web.git", None),                 # 다른 서버
])
def test_remote_path(tmp_path, url, expected):
    root = tmp_path / "repo"
    root.mkdir()
    for args in (["init", "-q"], ["remote", "add", "origin", url]):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    assert git_local.remote_path(root, "https://gitlab.acme.local/") == expected
    assert git_local.remote_path(tmp_path, "https://gitlab.acme.local/") is None   # 저장소 아님
