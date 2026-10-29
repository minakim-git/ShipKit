"""프로젝트 설정 편집 (GUI 용): 편집 중인 값을 검증하고, 비워 둔 값이 어디서 오는지 알려 준다.

값은 개인 defaults < 팀 공유 파일 < 개인 설정의 프로젝트 항목 순으로 덮어쓴다(config.py).
- 덮어쓰는 값(preset, rules, hooks, server 등): 위 층에 있으면 아래 층 값은 통째로 무시된다.
- 합치는 값(exclude, notices, deny, allow): 모든 층의 목록을 합친다.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from shipkit import PROJECT_FILE
from shipkit.config import (
    PRESET_ALIASES, PRESETS, Config, ConfigError, ProjectConfig, _resolve, load_project_file, parse_config,
)

SOURCE_SHARED = "팀 공유"
SOURCE_DEFAULTS = "기본값"
SOURCE_PRESET = "프리셋"


@dataclass
class Layers:
    """한 프로젝트의 설정 층. entry 는 개인 설정의 프로젝트 항목(편집 대상)."""
    name: str
    entry: Dict[str, Any]
    defaults: Dict[str, Any]
    shared: Dict[str, Any] = field(default_factory=dict)
    shared_path: Optional[Path] = None   # 팀 공유 파일 위치 (로컬 경로를 모르면 None)
    shared_error: Optional[str] = None

    @property
    def has_shared(self) -> bool:
        return self.shared_path is not None and self.shared_path.is_file()

    def inherited(self, key: str) -> Tuple[Any, Optional[str]]:
        """개인 항목에 key 가 없을 때 쓰이는 값과 그 출처."""
        for layer, source in ((self.shared, SOURCE_SHARED), (self.defaults, SOURCE_DEFAULTS)):
            if layer.get(key) not in (None, "", [], {}):
                return layer[key], source
        return None, None

    def inherited_list(self, key: str) -> List[Any]:
        """합치는 값(exclude, notices, deny, allow)에서 개인 항목 밖에서 오는 목록."""
        return list(self.defaults.get(key) or []) + list(self.shared.get(key) or [])

    def preset_name(self, entry: Optional[Dict[str, Any]] = None) -> Optional[str]:
        """실제로 쓰일 프리셋 이름 (config._parse_project 와 같은 규칙)."""
        entry = self.entry if entry is None else entry
        layers = [self.defaults, self.shared, entry]

        def get(key):
            for layer in reversed(layers):
                if key in layer:
                    return layer[key]
            return None

        name = get("preset")
        if name is None and not get("rules") and not get("build_dir"):
            name = "maven"
        name = PRESET_ALIASES.get(str(name), str(name)) if name else None
        return name if name in PRESETS else None

    def inherited_rules(self, entry: Optional[Dict[str, Any]] = None) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        if self.shared.get("rules"):
            return list(self.shared["rules"]), SOURCE_SHARED
        preset = self.preset_name(entry)
        if preset and PRESETS[preset].get("rules"):
            return [dict(r) for r in PRESETS[preset]["rules"]], f"{SOURCE_PRESET} {preset}"
        return [], None


def load_layers(data: Dict[str, Any], name: str, config_path: Path) -> Layers:
    entry = copy.deepcopy((data.get("projects") or {}).get(name) or {})
    defaults = data.get("defaults") or {}
    layers = Layers(name=name, entry=entry, defaults=defaults)
    root = entry.get("root") or defaults.get("root")
    if not root:
        return layers
    layers.shared_path = _resolve(root, config_path.parent) / PROJECT_FILE
    if layers.shared_path.is_file():
        try:
            layers.shared = load_project_file(layers.shared_path)
        except ConfigError as e:
            layers.shared_error = str(e)
    return layers


def with_entry(data: Dict[str, Any], name: str, entry: Dict[str, Any], old_name: Optional[str] = None) -> Dict[str, Any]:
    """설정 전체에서 한 프로젝트 항목만 바꾼 사본 (이름을 바꾸면 순서를 지킨다)."""
    trial = copy.deepcopy(data)
    projects = trial.get("projects") or {}
    old_name = old_name or name
    if name != old_name and name in projects:
        raise ConfigError(f"같은 이름의 프로젝트가 이미 있습니다: {name}")
    trial["projects"] = {(name if k == old_name else k): (entry if k == old_name else v) for k, v in projects.items()}
    if old_name not in projects:
        trial["projects"][name] = entry
    return trial


def draft_project(data: Dict[str, Any], name: str, entry: Dict[str, Any], config_path: Path,
                  old_name: Optional[str] = None) -> Tuple[Config, ProjectConfig]:
    """편집 중인 값으로 검증한 설정. 잘못되면 ConfigError."""
    if not name.strip():
        raise ConfigError("프로젝트 이름을 입력하세요.")
    cfg = parse_config(with_entry(data, name, entry, old_name), config_path)
    return cfg, cfg.projects[name]
