"""개인 설정 파일 읽기·쓰기 (GUI 용).

- 저장하기 전에 parse_config 로 검증해서, 잘못된 설정이 파일에 들어가지 않게 한다.
- 덮어쓰기 전에 이전 파일을 <이름>.bak 으로 남긴다. (YAML 주석은 저장할 때 사라진다)
- GUI 가 다루지 않는 키(hooks, rules, exclude 등)는 그대로 보존한다.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from shipkit import CONFIG_NAME, __app_name__, legacy
from shipkit.config import Config, ConfigError, parse_config

HEADER = (
    f"# {__app_name__} 개인 설정 ({CONFIG_NAME})\n"
    "# 프로그램 화면에서 저장하면 이 파일을 다시 씁니다. 저장 직전 내용은 같은 폴더의 .bak 파일에 남습니다.\n"
    "# 토큰은 이 파일에 적지 않습니다 (Windows 자격 증명 관리자에 저장).\n\n"
)


def load_raw(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        return {}
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"설정 파일 최상위는 매핑(key: value)이어야 합니다: {path}")
    return data


def validate(data: Dict[str, Any], path: Path) -> Config:
    return parse_config(data, path)


def save_raw(path: Path, data: Dict[str, Any]) -> Config:
    """검증 후 저장. 검증에 실패하면 ConfigError 를 내고 파일은 건드리지 않는다."""
    cfg = validate(data, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    text = HEADER + yaml.safe_dump(_clean(data), allow_unicode=True, sort_keys=False, default_flow_style=False)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    tmp.replace(path)
    return cfg


def _clean(value: Any) -> Any:
    """빈 문자열·None 값은 저장하지 않는다."""
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items() if v not in (None, "")}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    return value


# ---------------------------------------------------------------------- 프로젝트 편집
PROJECT_FIELDS = ("root", "output_dir", "preset", "server_os", "server_path", "gitlab_project", "github_repo")


def set_project(data: Dict[str, Any], name: str, values: Dict[str, Any], old_name: Optional[str] = None) -> None:
    """GUI 에서 편집하는 칸(PROJECT_FIELDS)만 바꾸고 나머지 키는 보존한다. 빈 값은 키를 지운다."""
    if not name.strip():
        raise ConfigError("프로젝트 이름을 입력하세요.")
    projects = data.setdefault("projects", {}) or {}
    data["projects"] = projects
    if old_name and old_name != name and name in projects:
        raise ConfigError(f"같은 이름의 프로젝트가 이미 있습니다: {name}")
    if not old_name and name in projects:
        raise ConfigError(f"같은 이름의 프로젝트가 이미 있습니다: {name}")
    entry = dict(projects.pop(old_name, {}) or {}) if old_name else {}
    for key in PROJECT_FIELDS:
        value = values.get(key)
        if value in (None, ""):
            entry.pop(key, None)
        else:
            entry[key] = value
    projects[name] = entry


def delete_project(data: Dict[str, Any], name: str) -> None:
    (data.get("projects") or {}).pop(name, None)


def merge_legacy(data: Dict[str, Any], db: Path) -> Tuple[List[str], List[str]]:
    """기존 툴(compiled-java-deployer) 프로젝트를 합친다. 반환: (추가한 이름, 이미 있어서 건너뛴 이름)."""
    gitlab_url, projects = legacy.read_legacy(db)
    imported = yaml.safe_load(legacy.to_yaml(gitlab_url, projects, db)) or {}
    if imported.get("gitlab") and not data.get("gitlab"):
        data["gitlab"] = imported["gitlab"]
    defaults = data.setdefault("defaults", {})
    for key, value in (imported.get("defaults") or {}).items():
        defaults.setdefault(key, value)
    current = data.setdefault("projects", {}) or {}
    data["projects"] = current
    added, skipped = [], []
    for name, entry in (imported.get("projects") or {}).items():
        if name in current:
            skipped.append(name)
        else:
            current[name] = entry
            added.append(name)
    return added, skipped
