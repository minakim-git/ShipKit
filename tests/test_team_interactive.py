"""3단계: 프로젝트 설정 파일(.shipkit.yml), 프로젝트 자동 인식, init-project, 대화형 모드."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from shipkit import PROJECT_FILE, cli
from shipkit.config import ConfigError, parse_config
from shipkit.interactive import Wizard, parse_mr_selection
from shipkit.projectfile import detect_preset

from conftest import write


def _write_project_file(root: Path, data: dict) -> None:
    write(root / PROJECT_FILE, yaml.safe_dump(data, allow_unicode=True))


# ---------------------------------------------------------------------- 설정 병합
def test_project_file_merge_order(tmp_path):
    root = tmp_path / "web"
    _write_project_file(root, {
        "preset": "vite", "server_path": "/var/www/web", "exclude": ["docs/**"],
        "hooks": {"post_apply": ["sudo systemctl reload nginx"]},
    })
    cfg = parse_config({
        "defaults": {"output_dir": str(tmp_path / "out" / "{project}"), "exclude": ["tmp/**"]},
        "projects": {
            "web": {"root": str(root)},
            "web-dev": {"root": str(root), "server_path": "/var/www/dev", "exclude": ["mock/**"]},
        },
    })
    web, dev = cfg.project("web"), cfg.project("web-dev")
    assert (web.preset, web.mode, web.server_path) == ("vite", "sync", "/var/www/web")
    assert web.project_file == root / PROJECT_FILE
    assert web.output_dir == (tmp_path / "out" / "web")
    assert dev.server_path == "/var/www/dev"            # 개인 설정 항목이 가장 우선
    assert dev.output_dir == (tmp_path / "out" / "web-dev")
    assert all(dev.is_excluded(p) for p in ("tmp/a", "docs/a", "mock/a"))  # 목록은 합친다
    assert [h.run for h in dev.hooks["post_apply"]] == ["sudo systemctl reload nginx"]


@pytest.mark.parametrize("key", ["root", "output_dir", "token"])
def test_project_file_rejects_personal_values(tmp_path, key):
    root = tmp_path / "app"
    _write_project_file(root, {"preset": "static", "server_path": "/srv", key: "x"})
    with pytest.raises(ConfigError, match="개인 설정"):
        parse_config({"projects": {"app": {"root": str(root), "output_dir": str(tmp_path / "o")}}})


def test_auto_detect_project_by_cwd_and_discovery(tmp_path):
    erp = tmp_path / "erp"
    api = tmp_path / "api"
    (erp / "src").mkdir(parents=True)
    _write_project_file(api, {"name": "my-api", "preset": "python", "server_path": "/srv/api"})
    (api / "app").mkdir()
    cfg = parse_config({
        "defaults": {"output_dir": str(tmp_path / "out" / "{project}")},
        "projects": {
            "erp": {"root": str(erp), "server_path": "/app/ROOT"},
            "other": {"root": str(tmp_path / "other"), "server_path": "/app/ROOT"},
        },
    })
    assert cfg.project(None, erp / "src").name == "erp"           # 등록된 프로젝트 안
    found = cfg.project(None, api / "app")                         # 등록 안 됐지만 .shipkit.yml 있음
    assert (found.name, found.preset, found.discovered) == ("my-api", "python", True)
    assert found.output_dir == tmp_path / "out" / "my-api"
    with pytest.raises(ConfigError, match="프로젝트 폴더 안에서"):
        cfg.project(None, tmp_path)


def test_discovery_needs_default_output_dir(tmp_path):
    api = tmp_path / "api"
    _write_project_file(api, {"preset": "python", "server_path": "/srv/api"})
    cfg = parse_config({"projects": {}})
    with pytest.raises(ConfigError, match="defaults.output_dir"):
        cfg.project(None, api)


# ---------------------------------------------------------------------- init-project
@pytest.mark.parametrize("files,expected", [
    ({"pom.xml": "<project/>"}, "maven"),
    ({"build.gradle.kts": ""}, "gradle"),
    ({"package.json": json.dumps({"devDependencies": {"vite": "^5"}})}, "vite"),
    ({"package.json": json.dumps({"devDependencies": {"@vue/cli-service": "^5"}})}, "vue-cli"),
    ({"manage.py": ""}, "python"),
    ({"index.html": ""}, "static"),
])
def test_detect_preset(tmp_path, files, expected):
    for name, content in files.items():
        write(tmp_path / name, content)
    assert detect_preset(tmp_path)[0] == expected


def test_init_project_command(tmp_path, capsys, monkeypatch):
    repo = tmp_path / "web"
    write(repo / "package.json", json.dumps({"devDependencies": {"vite": "^5"}}))
    (repo / ".git").mkdir()
    (repo / "src").mkdir()
    monkeypatch.chdir(repo / "src")
    assert cli.main(["init-project", "--server-path", "/var/www/web/", "--gitlab-project", "42"]) == 0
    data = yaml.safe_load((repo / PROJECT_FILE).read_text(encoding="utf-8"))
    assert data == {"name": "web", "preset": "vite", "server_path": "/var/www/web", "gitlab_project": 42}
    assert cli.main(["init-project"]) == 2  # 덮어쓰기 방지
    assert cli.main(["init-project", "--force", "--preset", "nope"]) == 2


# ---------------------------------------------------------------------- 대화형 모드
def test_parse_mr_selection():
    assert parse_mr_selection("1 3, !612 9 x", [101, 102, 103]) == [101, 103, 612]
    assert parse_mr_selection("", [1]) == []


def _answers(*items):
    it = iter(items)
    return lambda prompt: next(it)


def _cfg_two_projects(maven_project, tmp_path):
    return parse_config({
        "defaults": {"output_dir": str(tmp_path / "out" / "{project}")},
        "projects": {
            "erp": {"root": str(maven_project), "server_path": "/app/ROOT", "preset": "maven"},
            "zzz": {"root": str(tmp_path / "zzz"), "server_path": "/app/ROOT", "preset": "static"},
        },
    })


def test_wizard_files_flow_creates_package(maven_project, tmp_path):
    cfg = _cfg_two_projects(maven_project, tmp_path)
    out = []
    wizard = Wizard(cfg, ask=_answers(
        "",                                            # 프로젝트: 현재 폴더(erp)가 기본값
        "1",                                           # 출처: 파일 목록 (git 저장소 아님)
        "src/main/webapp/WEB-INF/views/user/list.jsp",
        "src/main/java/com/acme/User.java",
        "",                                            # 붙여넣기 끝
        "",                                            # 이대로 만들까요? -> 기본 Y
        "n",                                           # 압축 안 함
    ), out=lambda *a: out.append(" ".join(map(str, a))), cwd=maven_project / "src")
    assert wizard.run() == 0
    text = "\n".join(out)
    assert "[1/3] 프로젝트" in text and "erp" in text
    pkg = next((tmp_path / "out" / "erp").iterdir())
    assert (pkg / "files/WEB-INF/classes/com/acme/User.class").is_file()


def test_wizard_failure_needs_confirmation_and_quit(maven_project, tmp_path):
    cfg = _cfg_two_projects(maven_project, tmp_path)
    wizard = Wizard(cfg, ask=_answers("1", "1", "src/main/webapp/nope.jsp", "", "n"),
                    out=lambda *a: None, cwd=tmp_path)
    assert wizard.run() == 1
    assert not (tmp_path / "out").exists()
    quitter = Wizard(cfg, ask=_answers("q"), out=lambda *a: None, cwd=tmp_path)
    assert quitter.run() == 0


def test_wizard_sync_build_flow(tmp_path):
    root = tmp_path / "web"
    write(root / "dist/index.html", "<html/>")
    write(root / "dist/assets/app-1.js", "js")
    _write_project_file(root, {"preset": "vite", "server_path": "/var/www/web"})
    cfg = parse_config({"defaults": {"output_dir": str(tmp_path / "out" / "{project}")}, "projects": {}})
    wizard = Wizard(cfg, ask=_answers("", "", "y"), out=lambda *a: None, cwd=root)
    assert wizard.run() == 0                          # 프로젝트 1개 -> 자동 선택, 빌드 결과 전체, 압축 y
    out_dir = tmp_path / "out" / "web"
    assert any(p.suffixes[-2:] == [".tar", ".gz"] for p in out_dir.iterdir())


@pytest.mark.skipif(shutil.which("git") is None, reason="git 이 없습니다")
def test_wizard_git_flow_uses_latest_tag(maven_project, tmp_path):
    def git(*args):
        subprocess.run(["git", "-C", str(maven_project), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                       check=True, capture_output=True)

    git("init", "-q")
    git("add", "-A")
    git("commit", "-q", "-m", "init")
    git("tag", "v1")
    write(maven_project / "src/main/webapp/WEB-INF/views/user/list.jsp", "changed")
    git("commit", "-q", "-am", "change")
    cfg = _cfg_two_projects(maven_project, tmp_path)
    out = []
    wizard = Wizard(cfg, ask=_answers("1", "1", "", "", ""), out=lambda *a: out.append(" ".join(map(str, a))),
                    cwd=tmp_path)
    assert wizard.run() == 0
    pkg = next((tmp_path / "out" / "erp").iterdir())
    assert (pkg / "manifest.txt").read_text(encoding="utf-8") == "WEB-INF/views/user/list.jsp\n"


def test_no_args_without_tty_prints_help(capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    assert cli.main([]) == 2
    assert "init-project" in capsys.readouterr().out
