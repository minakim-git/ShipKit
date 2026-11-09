"""앱 설정 내보내기·가져오기.

개인 설정 파일(프로젝트, 서버 연결, GitLab·GitHub 주소, 패키지 기본값, 새 버전 확인 위치)과 화면 설정(테마, 글자 크기,
사이드바)을 YAML 파일 하나로 내보내고, 다른 PC 나 팀원이 가져온다.
토큰·비밀번호는 Windows 자격 증명 관리자에 있어 파일에 들어가지 않는다 (혹시 적혀 있어도 뺀다).
"""
from __future__ import annotations

import copy
import platform
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from shipkit import __app_name__, __version__, config_store
from shipkit.config import Config, ConfigError

EXPORT_KEY = "shipkit_settings"
EXPORT_VERSION = 1
UI_KEYS = ("theme", "theme_light", "theme_dark", "font_scale", "sidebar_collapsed")   # QSettings ui/* 중 내보내는 것
SECRET_KEYS = ("token", "password", "secret")
MODES = ("merge", "replace")
HEADER = (
    f"# {__app_name__} 설정 내보내기\n"
    "# 앱 설정 > 설정 파일 > [가져오기…] 로 읽습니다. 토큰·비밀번호는 들어 있지 않습니다.\n\n"
)


def strip_secrets(value: Any, where: str = "") -> Tuple[Any, List[str]]:
    """token / password / secret 키를 어디에 있든 뺀다. 반환: (뺀 뒤 값, 뺀 키 위치 목록)."""
    removed: List[str] = []
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            at = f"{where}.{key}" if where else str(key)
            if str(key).lower() in SECRET_KEYS:
                removed.append(at)
                continue
            cleaned, more = strip_secrets(item, at)
            result[key] = cleaned
            removed += more
        return result, removed
    if isinstance(value, list):
        result = []
        for i, item in enumerate(value):
            cleaned, more = strip_secrets(item, f"{where}[{i}]")
            result.append(cleaned)
            removed += more
        return result, removed
    return value, removed


def build(config_data: Dict[str, Any], ui: Dict[str, Any], when: Optional[datetime] = None) -> Dict[str, Any]:
    """내보낼 묶음. config_data 는 개인 설정 파일 내용(config_store.load_raw), ui 는 QSettings 의 ui/* 값."""
    cleaned, _removed = strip_secrets(copy.deepcopy(config_data))
    return {
        EXPORT_KEY: EXPORT_VERSION,
        "app": __version__,
        "exported_at": (when or datetime.now()).isoformat(timespec="seconds"),
        "computer": platform.node(),
        "config": cleaned,
        "ui": {k: v for k, v in ui.items() if k in UI_KEYS and v not in (None, "")},
    }


def write(path: Path, bundle: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = HEADER + yaml.safe_dump(bundle, allow_unicode=True, sort_keys=False, default_flow_style=False)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def read(path: Path) -> Dict[str, Any]:
    """내보낸 파일을 읽는다. 모양이 아니면 ConfigError."""
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except OSError as e:
        raise ConfigError(f"파일을 읽지 못했습니다: {e}") from e
    except yaml.YAMLError as e:
        raise ConfigError(f"YAML 문법 오류: {e}") from e
    if not isinstance(data, dict) or EXPORT_KEY not in data:
        raise ConfigError(f"{__app_name__} 설정 내보내기 파일이 아닙니다: {path.name}")
    try:
        version = int(data[EXPORT_KEY])
    except (TypeError, ValueError):
        raise ConfigError(f"파일 형식 번호가 올바르지 않습니다: {data[EXPORT_KEY]}") from None
    if version > EXPORT_VERSION:
        raise ConfigError(f"더 새 버전의 {__app_name__}(v{data.get('app', '?')})에서 내보낸 파일입니다. 앱을 올린 뒤 가져오세요.")
    config = data.get("config") or {}
    if not isinstance(config, dict):
        raise ConfigError("config 항목은 매핑(key: value)이어야 합니다.")
    config, removed = strip_secrets(config)
    data["config"] = config
    data["stripped"] = removed
    ui = data.get("ui") or {}
    data["ui"] = {k: v for k, v in ui.items() if k in UI_KEYS} if isinstance(ui, dict) else {}
    return data


@dataclass
class Summary:
    """가져오기 전에 보여 줄 내용."""
    projects: List[str]
    servers: int                                  # 모든 프로젝트의 서버 연결 수
    services: List[str]                           # gitlab / github
    defaults: Dict[str, Any]
    update_location: str
    ui: Dict[str, Any]
    missing_roots: List[Tuple[str, str]] = field(default_factory=list)   # 이 PC 에 없는 로컬 경로 (이름, 경로)
    exported_at: str = ""
    computer: str = ""
    app: str = ""

    def lines(self) -> List[str]:
        out = [f"프로젝트 {len(self.projects)}개" + (f": {', '.join(self.projects[:6])}" + (" …" if len(self.projects) > 6 else "")
                                                   if self.projects else "")]
        if self.servers:
            out.append(f"서버 연결 {self.servers}개")
        if self.services:
            out.append("연결: " + ", ".join({"gitlab": "GitLab", "github": "GitHub"}.get(s, s) for s in self.services))
        if self.defaults:
            out.append("패키지 기본값: " + ", ".join(f"{k} = {v}" for k, v in self.defaults.items()))
        if self.update_location:
            out.append(f"새 버전 확인 위치: {self.update_location}")
        if self.ui:
            out.append("화면 설정: " + ", ".join(f"{k} = {v}" for k, v in self.ui.items()))
        return out


def summarize(bundle: Dict[str, Any]) -> Summary:
    config = bundle.get("config") or {}
    projects = config.get("projects") or {}
    if not isinstance(projects, dict):
        projects = {}
    servers = sum(len(p.get("servers") or []) for p in projects.values() if isinstance(p, dict))
    missing = []
    for name, p in projects.items():
        root = (p or {}).get("root") if isinstance(p, dict) else None
        if root and not Path(str(root)).is_dir():
            missing.append((str(name), str(root)))
    defaults = config.get("defaults") or {}
    return Summary(
        projects=[str(n) for n in projects], servers=servers,
        services=[s for s in ("gitlab", "github") if s in config],
        defaults=dict(defaults) if isinstance(defaults, dict) else {},
        update_location=str(config.get("update_location") or ""), ui=dict(bundle.get("ui") or {}),
        missing_roots=missing, exported_at=str(bundle.get("exported_at") or ""),
        computer=str(bundle.get("computer") or ""), app=str(bundle.get("app") or ""),
    )


def merge(current: Dict[str, Any], incoming: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str], List[str]]:
    """합치기: 없는 프로젝트만 더하고(같은 이름은 건너뜀), gitlab/github·기본값·새 버전 위치는 비어 있을 때만 채운다.
    반환: (합친 설정, 더한 프로젝트, 건너뛴 프로젝트)."""
    data = copy.deepcopy(current)
    for key in ("gitlab", "github", "update_location"):
        if key in incoming and key not in data:
            data[key] = copy.deepcopy(incoming[key])
    defaults = data.setdefault("defaults", {}) or {}
    data["defaults"] = defaults
    for key, value in (incoming.get("defaults") or {}).items():
        defaults.setdefault(key, value)
    projects = data.setdefault("projects", {}) or {}
    data["projects"] = projects
    added, skipped = [], []
    for name, entry in (incoming.get("projects") or {}).items():
        if name in projects:
            skipped.append(str(name))
        else:
            projects[name] = copy.deepcopy(entry)
            added.append(str(name))
    return data, added, skipped


def apply(config_path: Path, bundle: Dict[str, Any], mode: str) -> Tuple[Config, List[str], List[str]]:
    """가져오기. mode: merge(합치기) / replace(지금 설정을 파일 내용으로 바꾸기, 이전 파일은 .bak).
    저장 전에 검증하므로 잘못된 설정이면 ConfigError 가 나고 파일은 그대로다. 반환: (설정, 더한 프로젝트, 건너뛴 프로젝트)."""
    if mode not in MODES:
        raise ValueError(mode)
    incoming = bundle.get("config") or {}
    if mode == "replace":
        data, added, skipped = copy.deepcopy(incoming), [str(n) for n in (incoming.get("projects") or {})], []
    else:
        data, added, skipped = merge(config_store.load_raw(config_path), incoming)
    cfg = config_store.save_raw(config_path, data)
    return cfg, added, skipped
