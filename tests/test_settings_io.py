"""앱 설정 내보내기·가져오기: 비밀 제외, 요약, 합치기/바꾸기, 잘못된 파일."""
from datetime import datetime

import pytest
import yaml

from shipkit import config_store, settings_io
from shipkit.config import ConfigError


def _personal(tmp_path, maven_project):
    return {
        "defaults": {"output_dir": (tmp_path / "out" / "{project}").as_posix(), "package_name": "%Y%m%d-%H%M"},
        "gitlab": {"url": "https://gitlab.acme.local", "token_env": "GITLAB_TOKEN"},
        "update_location": r"\\fileserver\tools\ShipKit",
        "projects": {"erp": {"root": maven_project.as_posix(), "server_path": "/app/ROOT",
                             "servers": [{"name": "운영", "host": "prod", "user": "app", "password": "x"}]}},
    }


def test_export_strips_secrets_and_round_trips(tmp_path, maven_project):
    data = _personal(tmp_path, maven_project)
    data["github"] = {"url": "https://api.github.com", "token": "ghp_secret"}
    bundle = settings_io.build(data, {"theme": "sky", "font_scale": "large", "geometry": "x", "sidebar_collapsed": "true"},
                               when=datetime(2026, 11, 9, 10, 0))
    assert bundle[settings_io.EXPORT_KEY] == 1 and bundle["exported_at"] == "2026-11-09T10:00:00"
    assert "token" not in bundle["config"]["github"] and bundle["config"]["gitlab"]["token_env"] == "GITLAB_TOKEN"
    assert "password" not in bundle["config"]["projects"]["erp"]["servers"][0]
    assert bundle["ui"] == {"theme": "sky", "font_scale": "large", "sidebar_collapsed": "true"}   # geometry 는 안 나간다
    assert data["github"]["token"] == "ghp_secret"                                                # 원본은 건드리지 않는다

    path = tmp_path / "export.yaml"
    settings_io.write(path, bundle)
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# ShipKit 설정 내보내기") and "ghp_secret" not in text and "password" not in text
    back = settings_io.read(path)
    assert back["config"] == bundle["config"] and back["ui"] == bundle["ui"] and back["stripped"] == []

    summary = settings_io.summarize(back)
    assert summary.projects == ["erp"] and summary.servers == 1 and summary.services == ["gitlab", "github"]
    assert summary.update_location == r"\\fileserver\tools\ShipKit" and summary.missing_roots == []
    assert summary.lines()[0] == "프로젝트 1개: erp" and "서버 연결 1개" in summary.lines()
    assert "연결: GitLab, GitHub" in summary.lines() and summary.computer == bundle["computer"]


def test_read_rejects_wrong_files_and_strips_secrets(tmp_path):
    other = tmp_path / "other.yaml"
    other.write_text("defaults: {}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="설정 내보내기 파일이 아닙니다"):
        settings_io.read(other)
    newer = tmp_path / "newer.yaml"
    newer.write_text(yaml.safe_dump({settings_io.EXPORT_KEY: 99, "app": "9.0.0", "config": {}}), encoding="utf-8")
    with pytest.raises(ConfigError, match="더 새 버전"):
        settings_io.read(newer)
    broken = tmp_path / "broken.yaml"
    broken.write_text("shipkit_settings: 1\nconfig: [1, 2]\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="매핑"):
        settings_io.read(broken)
    with pytest.raises(ConfigError, match="YAML"):
        bad = tmp_path / "bad.yaml"
        bad.write_text("shipkit_settings: [\n", encoding="utf-8")
        settings_io.read(bad)
    with pytest.raises(ConfigError, match="읽지 못했습니다"):
        settings_io.read(tmp_path / "missing.yaml")

    leaked = tmp_path / "leaked.yaml"                 # 손으로 고쳐 비밀을 넣어도 가져올 때 뺀다
    leaked.write_text(yaml.safe_dump({settings_io.EXPORT_KEY: 1, "config": {"gitlab": {"url": "u", "token": "t"}},
                                      "ui": {"theme": "sky", "geometry": "x"}}), encoding="utf-8")
    data = settings_io.read(leaked)
    assert data["config"] == {"gitlab": {"url": "u"}} and data["stripped"] == ["gitlab.token"] and data["ui"] == {"theme": "sky"}

    summary = settings_io.summarize({"config": {"projects": {"x": {"root": str(tmp_path / "nowhere")}}}})
    assert summary.missing_roots == [("x", str(tmp_path / "nowhere"))] and summary.lines() == ["프로젝트 1개: x"]


def test_merge_and_replace(tmp_path, maven_project):
    data = _personal(tmp_path, maven_project)
    path = tmp_path / "shipkit.yaml"
    config_store.save_raw(path, {"defaults": {"output_dir": (tmp_path / "mine" / "{project}").as_posix()},
                                 "projects": {"erp": {"root": maven_project.as_posix(), "server_path": "/app/MINE"},
                                              "crm": {"root": maven_project.as_posix(), "server_path": "/app/CRM"}}})
    incoming = settings_io.build(data, {})
    incoming["config"]["projects"]["hr"] = {"root": maven_project.as_posix(), "server_path": "/app/HR"}

    cfg, added, skipped = settings_io.apply(path, incoming, "merge")
    assert added == ["hr"] and skipped == ["erp"]
    saved = config_store.load_raw(path)
    assert saved["projects"]["erp"]["server_path"] == "/app/MINE"            # 같은 이름은 내 것을 지킨다
    assert set(saved["projects"]) == {"erp", "crm", "hr"} and saved["gitlab"]["url"] == "https://gitlab.acme.local"
    assert saved["defaults"] == {"output_dir": (tmp_path / "mine" / "{project}").as_posix(), "package_name": "%Y%m%d-%H%M"}
    assert saved["update_location"] == r"\\fileserver\tools\ShipKit" and sorted(cfg.projects) == ["crm", "erp", "hr"]

    cfg, added, skipped = settings_io.apply(path, incoming, "replace")
    assert added == ["erp", "hr"] and skipped == []
    saved = config_store.load_raw(path)
    assert set(saved["projects"]) == {"erp", "hr"} and saved["projects"]["erp"]["server_path"] == "/app/ROOT"
    assert "crm" in config_store.load_raw(path.with_name(path.name + ".bak"))["projects"]   # 이전 파일은 .bak

    bad = settings_io.build({"projects": {"erp": {"root": maven_project.as_posix(), "server_path": "relative/x"}}}, {})
    with pytest.raises(ConfigError):
        settings_io.apply(path, bad, "replace")
    assert set(config_store.load_raw(path)["projects"]) == {"erp", "hr"}     # 검증 실패면 파일 그대로
    with pytest.raises(ValueError):
        settings_io.apply(path, incoming, "wrong")
