"""기존 툴 호환(폴더명, 설정 가져오기)과 작업 폴더 안전장치 테스트."""
import shutil
import sqlite3
import subprocess
from datetime import datetime

import pytest
import yaml

from shipkit import cli, config, legacy
from shipkit.config import ConfigError, parse_config

from conftest import write


def _project(**over):
    p = {"root": "D:/w/erp", "output_dir": "D:/deploy", "server_path": "/app/ROOT"}
    p.update(over)
    return parse_config({"projects": {"erp": p}}).project("erp")


def test_package_name_default_and_custom():
    when = datetime(2026, 10, 26, 11, 33, 30)
    assert _project().render_package_name(when) == "2026-10-26-1133"
    assert _project(package_name="{project}_%Y%m%d_%H%M%S").render_package_name(when) == "erp_20261026_113330"


@pytest.mark.parametrize("bad", ["%Y/%m", "a:b", "", "..", "%Y?"])
def test_package_name_rejects_unsafe(bad):
    with pytest.raises(ConfigError, match="package_name"):
        _project(package_name=bad)


def test_user_config_fallback(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SHIPKIT_CONFIG", raising=False)
    user_cfg = tmp_path / "appdata" / "shipkit.yaml"
    monkeypatch.setattr(config, "user_config_path", lambda: user_cfg)
    with pytest.raises(ConfigError, match="설정 파일이 없습니다"):
        config.find_config(None)
    write(user_cfg, "x")
    assert config.find_config(None) == user_cfg
    write(tmp_path / "shipkit.yaml", "x")  # 현재 폴더 설정이 우선
    assert config.find_config(None) == tmp_path / "shipkit.yaml"


def _legacy_db(path):
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE projects (id INTEGER PRIMARY KEY, name TEXT, local_path TEXT, package_path TEXT,
            server_ip TEXT, server_port TEXT, server_username TEXT, server_password TEXT,
            server_path TEXT, gitlab_project_id TEXT);
        CREATE TABLE settings (id INTEGER PRIMARY KEY, key TEXT UNIQUE, value TEXT);
        INSERT INTO projects (name, local_path, package_path, server_password, server_path, gitlab_project_id)
        VALUES ('acme_erp', 'C:/workspace\\acme_erp',
                'D:/배포/acme_erp/반영', 'SECRET-PW', '/opt/acme/app/erp', '306'),
               ('no_server', 'C:/x', 'D:/y', '', '', '');
        INSERT INTO settings (key, value) VALUES ('gitlab_url', 'https://gitlab.acme.local/'),
               ('gitlab_token', 'SECRET-TOKEN');
    """)
    con.commit()
    con.close()


def test_import_legacy(tmp_path, capsys):
    db = tmp_path / "database.db"
    _legacy_db(db)
    out = tmp_path / "shipkit.yaml"
    assert cli.main(["import-legacy", "--db", str(db), "--out", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    assert "SECRET" not in text  # 비밀번호·토큰은 가져오지 않는다
    data = yaml.safe_load(text)
    assert data["gitlab"]["url"] == "https://gitlab.acme.local"
    p = data["projects"]["acme_erp"]
    assert p == {
        "root": "C:/workspace/acme_erp",
        "output_dir": "D:/배포/acme_erp/반영",
        "server_path": "/opt/acme/app/erp",
        "gitlab_project": "306",
    }
    assert "no_server" not in data["projects"]  # 서버 경로 없는 프로젝트는 주석 처리
    assert "# [확인 필요] server_path" in text
    assert cli.main(["import-legacy", "--db", str(db), "--out", str(out)]) == 2  # 덮어쓰기 방지


@pytest.mark.skipif(shutil.which("git") is None, reason="git 이 없습니다")
def test_git_mode_warns_dirty_and_head_mismatch(maven_project, tmp_path, capsys):
    def git(*args):
        subprocess.run(["git", "-C", str(maven_project), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                       check=True, capture_output=True)

    jsp = maven_project / "src/main/webapp/WEB-INF/views/user/list.jsp"
    git("init", "-q")
    git("add", "-A")
    git("commit", "-q", "-m", "init")
    git("tag", "v1")
    write(jsp, "release")
    git("commit", "-q", "-am", "release")
    git("tag", "v2")
    write(maven_project / "src/main/webapp/js/한글 파일.js", "next")
    git("commit", "-q", "-am", "next work")  # HEAD 가 v2 보다 앞섬
    write(jsp, "local test edit")  # 커밋 안 한 수정

    cfg = tmp_path / "shipkit.yaml"
    cfg.write_text(yaml.safe_dump({"projects": {"erp": {
        "root": maven_project.as_posix(), "output_dir": (tmp_path / "out").as_posix(), "server_path": "/app/ROOT",
    }}}), encoding="utf-8")

    code = cli.main(["package", "-c", str(cfg), "--git", "v1..v2", "--dry-run"])
    out = capsys.readouterr().out
    assert code == 0
    assert "범위 끝 v2" in out
    assert "커밋되지 않은 로컬 수정" in out

    code = cli.main(["package", "-c", str(cfg), "--git", "v1..v2", "--strict"])
    assert code == 1 and not (tmp_path / "out").exists()
