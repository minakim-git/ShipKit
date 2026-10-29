"""프로젝트 설정 편집: 물려받는 값의 출처, 편집 중인 항목 바꿔 끼우기."""
import pytest
import yaml

from shipkit import PROJECT_FILE, config_store, project_edit
from shipkit.config import ConfigError

from conftest import write


def _setup(tmp_path, entry, shared=None):
    root = tmp_path / "repo"
    root.mkdir()
    if shared is not None:
        write(root / PROJECT_FILE, yaml.safe_dump(shared))
    path = tmp_path / "shipkit.yaml"
    data = {"defaults": {"output_dir": "D:/deploy/{project}", "server_os": "linux"},
            "projects": {"erp": dict({"root": root.as_posix()}, **entry)}}
    config_store.save_raw(path, data)
    return path, root, config_store.load_raw(path)


def test_inherited_sources(tmp_path):
    path, root, data = _setup(tmp_path, {}, shared={"server_path": "/app", "preset": "vite"})
    layers = project_edit.load_layers(data, "erp", path)
    assert layers.has_shared and layers.inherited("server_path") == ("/app", "팀 공유")
    assert layers.inherited("server_os") == ("linux", "기본값") and layers.inherited("gitlab_project") == (None, None)
    assert layers.preset_name() == "vite" and layers.inherited_rules() == ([], None)
    assert layers.preset_name({"preset": "maven"}) == "maven"
    assert layers.inherited_rules({"preset": "maven"})[1] == "프리셋 maven"


def test_with_entry_keeps_order_and_rejects_duplicate(tmp_path):
    data = {"projects": {"a": {"x": 1}, "b": {"x": 2}}}
    assert list(project_edit.with_entry(data, "a2", {"x": 3}, "a")["projects"]) == ["a2", "b"]
    with pytest.raises(ConfigError, match="이미 있습니다"):
        project_edit.with_entry(data, "b", {}, "a")
