from pathlib import Path

import pytest

from shipkit.config import ConfigError, parse_config
from shipkit.models import ChangeType as C, FileChange, merge_changes, normalize_repo_path


@pytest.mark.parametrize("raw,expected", [
    ("src\\main\\java\\A.java", "src/main/java/A.java"),
    ("./src/a.jsp ", "src/a.jsp"),
    ("src/x/../a.jsp", "src/a.jsp"),
])
def test_normalize_repo_path(raw, expected):
    assert normalize_repo_path(raw) == expected


@pytest.mark.parametrize("raw", ["/etc/passwd", "C:/x/a.java", "../a.java", "a/../../b", "  "])
def test_normalize_repo_path_rejects(raw):
    with pytest.raises(ValueError):
        normalize_repo_path(raw)


def test_merge_changes_keeps_final_state():
    first = [FileChange("a", C.ADDED, "!1"), FileChange("b", C.MODIFIED, "!1"), FileChange("c", C.DELETED, "!1")]
    second = [FileChange("a", C.DELETED, "!2"), FileChange("b", C.DELETED, "!2"), FileChange("c", C.ADDED, "!2"),
              FileChange("d", C.MODIFIED, "!2")]
    merged = {c.path: c for c in merge_changes([first, second])}
    assert "a" not in merged                      # 추가 후 삭제 -> 상쇄
    assert merged["b"].change == C.DELETED
    assert merged["c"].change == C.MODIFIED       # 삭제 후 재추가 -> 서버 입장에선 수정
    assert merged["c"].origin == "!1,!2"
    assert merged["d"].change == C.MODIFIED


def _cfg(**over):
    p = {"root": "D:/w/erp", "output_dir": "D:/deploy", "server_path": "/app/ROOT/"}
    p.update(over)
    return {"projects": {"erp": p}}


def test_parse_config_defaults_to_maven():
    cfg = parse_config(_cfg())
    p = cfg.project(None)
    assert p.server_path == "/app/ROOT"
    assert p.rules[0].build_dir == "target/classes"
    assert p.modules == [""]


def test_gradle_preset_and_defaults_merge():
    cfg = parse_config({"defaults": {"preset": "gradle", "exclude": ["**/*.md"]}, **_cfg()})
    p = cfg.project("erp")
    assert p.rules[0].build_dir == "build/classes/java/main"
    assert p.is_excluded("README.md") and p.is_excluded("docs/a.md")


@pytest.mark.parametrize("server_path", ["app/ROOT", "/app/$HOME", '/app/"x"', "/app/`id`", "/app/'q'"])
def test_server_path_validation(server_path):
    with pytest.raises(ConfigError):
        parse_config(_cfg(server_path=server_path))


def test_plain_token_is_rejected():
    with pytest.raises(ConfigError, match="평문"):
        parse_config({"gitlab": {"url": "https://g", "token": "glpat-x"}, **_cfg()})


def test_unknown_project():
    cfg = parse_config(_cfg())
    with pytest.raises(ConfigError, match="erp"):
        cfg.project("nope")
