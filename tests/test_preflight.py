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


def _shared_repo(maven_project, tmp_path):
    """원격(bare) + 내 저장소(maven_project) + 팀원 저장소(other)."""
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    git(maven_project, "init", "-q", "-b", "main")
    git(maven_project, "add", "-A")
    git(maven_project, "commit", "-q", "-m", "init")
    git(maven_project, "remote", "add", "origin", str(remote))
    git(maven_project, "push", "-q", "-u", "origin", "main")
    other = tmp_path / "other"
    git(tmp_path, "clone", "-q", str(remote), str(other))
    return other


def _teammate_commit(other, rel, text):
    write(other / rel, text)
    git(other, "commit", "-q", "-am", f"change {rel}")
    git(other, "push", "-q")


def test_pull_ff(maven_project, tmp_path):
    other = _shared_repo(maven_project, tmp_path)
    _teammate_commit(other, "pom.xml", "<project>v2</project>")
    project = make_config(maven_project, tmp_path / "out").projects["erp"]
    behind = [c for c in preflight.run(None, project) if c.text.startswith("원격보다")][0]
    assert behind.action == preflight.ACTION_PULL

    write(maven_project / "pom.xml", "<project>my edit</project>")    # 받을 파일과 겹치는 수정 -> 멈춤
    head = git_local.run_git(maven_project, "rev-parse", "HEAD")
    with pytest.raises(git_local.GitError, match="겹쳐서"):
        git_local.pull_ff(maven_project)
    assert git_local.run_git(maven_project, "rev-parse", "HEAD") == head

    git(maven_project, "checkout", "-q", "--", "pom.xml")
    write(maven_project / "README.md", "unrelated local edit")          # 겹치지 않는 수정은 그대로 두고 받음
    assert git_local.pull_ff(maven_project) == 1
    assert (maven_project / "pom.xml").read_text(encoding="utf-8") == "<project>v2</project>"
    assert (maven_project / "README.md").read_text(encoding="utf-8") == "unrelated local edit"
    assert git_local.pull_ff(maven_project) == 0                          # 이미 최신


def test_pull_ff_refuses_diverged(maven_project, tmp_path):
    other = _shared_repo(maven_project, tmp_path)
    _teammate_commit(other, "pom.xml", "<project>v2</project>")
    git(maven_project, "commit", "-q", "--allow-empty", "-m", "local only")
    with pytest.raises(git_local.GitError, match="갈라졌습니다"):
        git_local.pull_ff(maven_project)


def _mr(iid, merge_sha, head_sha=""):
    return {"iid": iid, "ref": f"!{iid}", "commit_sha": merge_sha, "head_sha": head_sha}


def _sha(root):
    return git_local.run_git(root, "rev-parse", "HEAD").strip()


def test_request_commit_warnings(maven_project, tmp_path):
    """작업 브랜치 -> develop(개발서버) / -> main(운영서버) 흐름. develop 에서 배포할 때
    main 으로 간 MR 이라도 작업 커밋이 develop 에 있으면 경고하지 않는다. 없을 때만 짧게 알린다."""
    from shipkit import workflow
    from shipkit.models import SourceInfo
    other = _shared_repo(maven_project, tmp_path)                    # main 하나로 시작
    git(other, "checkout", "-q", "-b", "develop")
    git(other, "push", "-q", "-u", "origin", "develop")

    git(other, "checkout", "-q", "-b", "feature/a", "develop")        # 작업 브랜치를 develop 과 main 에 각각 머지
    _commit_only(other, "pom.xml", "<project>a</project>")
    head_a = _sha(other)
    for target in ("develop", "main"):
        git(other, "checkout", "-q", target)
        git(other, "merge", "-q", "--no-ff", "-m", f"Merge feature/a into {target}", "feature/a")
        git(other, "push", "-q", "origin", target)
    merge_a_main = _sha(other)

    git(other, "checkout", "-q", "-b", "feature/b", "main")           # main 에만 머지된 작업
    _commit_only(other, "README.md", "b")
    head_b = _sha(other)
    git(other, "checkout", "-q", "main")
    git(other, "merge", "-q", "--no-ff", "-m", "Merge feature/b", "feature/b")
    git(other, "push", "-q", "origin", "main")
    merge_b = _sha(other)

    git(maven_project, "fetch", "-q")
    git(maven_project, "checkout", "-q", "-b", "develop", "origin/develop")
    git(other, "checkout", "-q", "develop")                           # develop 에 새로 올라온, 아직 안 받은 MR
    _commit_only(other, "pom.xml", "<project>c</project>")
    head_c = _sha(other)
    git(other, "push", "-q", "origin", "develop")
    git(maven_project, "fetch", "-q")

    project = make_config(maven_project, tmp_path / "out").projects["erp"]
    source = SourceInfo("gitlab-mr", "t", merge_requests=[
        _mr(1, merge_a_main, head_a), _mr(2, merge_b, head_b), _mr(3, head_c, head_c), _mr(4, "0" * 40)])
    workflow.check_local_has_requests(project, source)
    assert source.warnings == [                                       # !1 은 develop 에 작업 커밋이 있어 경고 없음
        "MR !3: 아직 pull 하지 않은 커밋입니다 → [pull 받기]",
        "MR !4: 로컬에 없는 커밋입니다 → [pull 받기]",
        "MR !2: develop 에 머지되지 않았습니다",
    ]


def _commit_only(root, rel, text):
    write(root / rel, text)
    git(root, "commit", "-q", "-am", f"change {rel}")
