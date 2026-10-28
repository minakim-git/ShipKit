"""YAML 설정 파일 로더.

설정 파일 탐색 순서: --config 인자 > 환경변수 SHIPKIT_CONFIG > 현재 폴더의 shipkit.yaml
"""
from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

DEFAULT_CONFIG_NAME = "shipkit.yaml"
# 패키지 폴더 이름 (strftime 형식, {project} = 프로젝트 이름). 기존 툴과 같은 2026-10-26-1133 형태.
DEFAULT_PACKAGE_NAME = "%Y-%m-%d-%H%M"
_UNSAFE_DIR_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

PRESETS: Dict[str, List[Dict[str, str]]] = {
    "maven": [
        {"src": "src/main/java", "dest": "WEB-INF/classes", "build_dir": "target/classes"},
        {"src": "src/main/resources", "dest": "WEB-INF/classes"},
        {"src": "src/main/webapp", "dest": ""},
    ],
    "gradle": [
        {"src": "src/main/java", "dest": "WEB-INF/classes", "build_dir": "build/classes/java/main"},
        {"src": "src/main/resources", "dest": "WEB-INF/classes"},
        {"src": "src/main/webapp", "dest": ""},
    ],
}

# shipkit.sh 에 그대로 들어가므로 셸이 해석할 수 있는 문자는 허용하지 않는다.
_UNSAFE_SHELL_CHARS = re.compile(r'["\'$`\\\n\r]')


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Rule:
    src: str               # 모듈 기준 소스 경로 접두어 (예: src/main/java)
    dest: str              # 서버 배포 경로 기준 대상 접두어 (예: WEB-INF/classes, "" = 루트)
    build_dir: Optional[str] = None  # 지정 시 소스 대신 빌드 산출물을 복사 (.java -> .class)


@dataclass
class GitLabConfig:
    url: str
    token_env: str = "GITLAB_TOKEN"
    verify_ssl: bool = True
    ca_bundle: Optional[str] = None
    timeout: float = 15.0


@dataclass
class ProjectConfig:
    name: str
    root: Path
    output_dir: Path
    server_path: str
    gitlab_project: Optional[str] = None
    modules: List[str] = field(default_factory=lambda: ["."])
    rules: List[Rule] = field(default_factory=list)
    exclude: List[str] = field(default_factory=list)
    source_encodings: List[str] = field(default_factory=lambda: ["utf-8", "cp949"])
    package_name: str = DEFAULT_PACKAGE_NAME

    def is_excluded(self, path: str) -> bool:
        return any(glob_match(path, pat) for pat in self.exclude)

    def render_package_name(self, when: datetime) -> str:
        return when.strftime(self.package_name.replace("{project}", self.name.replace("%", "%%")))


@dataclass
class Config:
    path: Optional[Path]
    gitlab: Optional[GitLabConfig]
    projects: Dict[str, ProjectConfig]

    def project(self, name: Optional[str]) -> ProjectConfig:
        if name is None:
            if len(self.projects) == 1:
                return next(iter(self.projects.values()))
            raise ConfigError(
                "프로젝트를 지정해주세요 (-p). 등록된 프로젝트: " + ", ".join(sorted(self.projects))
            )
        if name not in self.projects:
            raise ConfigError(
                f"설정에 없는 프로젝트입니다: {name}. 등록된 프로젝트: " + ", ".join(sorted(self.projects))
            )
        return self.projects[name]


def glob_match(path: str, pattern: str) -> bool:
    """fnmatch 기반 매칭. '**/x' 는 루트의 'x' 도 매칭한다."""
    if fnmatch.fnmatchcase(path, pattern):
        return True
    return pattern.startswith("**/") and fnmatch.fnmatchcase(path, pattern[3:])


def user_config_path() -> Path:
    """사용자 공용 설정 위치: Windows 는 %APPDATA%\\shipkit\\shipkit.yaml"""
    base = os.environ.get("APPDATA") if os.name == "nt" else None
    if base:
        return Path(base) / "shipkit" / DEFAULT_CONFIG_NAME
    return Path.home() / ".config" / "shipkit" / DEFAULT_CONFIG_NAME


def find_config(explicit: Optional[str]) -> Path:
    """설정 파일 탐색: --config > SHIPKIT_CONFIG > ./shipkit.yaml > 사용자 공용 설정"""
    if explicit:
        p = Path(explicit)
        if not p.is_file():
            raise ConfigError(f"설정 파일을 찾을 수 없습니다: {p}")
        return p
    env = os.environ.get("SHIPKIT_CONFIG")
    if env:
        p = Path(env)
        if not p.is_file():
            raise ConfigError(f"SHIPKIT_CONFIG 가 가리키는 설정 파일이 없습니다: {p}")
        return p
    for p in (Path.cwd() / DEFAULT_CONFIG_NAME, user_config_path()):
        if p.is_file():
            return p
    raise ConfigError(
        f"설정 파일이 없습니다. 'shipkit init' 으로 {user_config_path()} 를 만들거나 --config 로 지정하세요."
    )


def load_config(explicit: Optional[str] = None) -> Config:
    path = find_config(explicit)
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"설정 파일 YAML 문법 오류 ({path}): {e}") from e
    return parse_config(data, path)


def parse_config(data: Dict[str, Any], path: Optional[Path] = None) -> Config:
    if not isinstance(data, dict):
        raise ConfigError("설정 파일 최상위는 매핑(key: value)이어야 합니다.")
    base_dir = path.parent if path else Path.cwd()

    gitlab = None
    if data.get("gitlab"):
        g = _require_dict(data["gitlab"], "gitlab")
        url = str(g.get("url") or "").strip()
        if not url:
            raise ConfigError("gitlab.url 이 비어 있습니다.")
        if "token" in g:
            raise ConfigError(
                "gitlab.token 은 지원하지 않습니다(평문 저장 금지). "
                "환경변수(gitlab.token_env) 또는 'shipkit token set' 을 사용하세요."
            )
        gitlab = GitLabConfig(
            url=url.rstrip("/"),
            token_env=str(g.get("token_env") or "GITLAB_TOKEN"),
            verify_ssl=bool(g.get("verify_ssl", True)),
            ca_bundle=_opt_str(g.get("ca_bundle")),
            timeout=float(g.get("timeout", 15)),
        )

    defaults = _require_dict(data.get("defaults") or {}, "defaults")
    raw_projects = _require_dict(data.get("projects") or {}, "projects")
    if not raw_projects:
        raise ConfigError("projects 에 프로젝트가 하나 이상 있어야 합니다.")

    projects = {}
    for name, raw in raw_projects.items():
        projects[str(name)] = _parse_project(str(name), _require_dict(raw, f"projects.{name}"), defaults, base_dir)
    return Config(path=path, gitlab=gitlab, projects=projects)


def _parse_project(name: str, raw: Dict[str, Any], defaults: Dict[str, Any], base_dir: Path) -> ProjectConfig:
    where = f"projects.{name}"

    def get(key: str, default: Any = None) -> Any:
        return raw.get(key, defaults.get(key, default))

    root = get("root")
    if not root:
        raise ConfigError(f"{where}.root (프로젝트 로컬 경로)가 필요합니다.")
    output_dir = get("output_dir")
    if not output_dir:
        raise ConfigError(f"{where}.output_dir (패키지 생성 경로)가 필요합니다.")

    server_path = str(get("server_path") or "").strip()
    if not server_path:
        raise ConfigError(f"{where}.server_path (서버 배포 경로, 예: /app/tomcat/webapps/ROOT)가 필요합니다.")
    if not server_path.startswith("/"):
        raise ConfigError(f"{where}.server_path 는 '/' 로 시작하는 리눅스 절대 경로여야 합니다: {server_path}")
    if _UNSAFE_SHELL_CHARS.search(server_path):
        raise ConfigError(f"{where}.server_path 에 사용할 수 없는 문자(\" ' $ ` \\)가 있습니다.")
    server_path = server_path.rstrip("/") or "/"

    if raw.get("rules"):
        rules_raw = raw["rules"]
    else:
        preset = str(get("preset", "maven"))
        if preset not in PRESETS:
            raise ConfigError(f"{where}.preset 은 {', '.join(PRESETS)} 중 하나여야 합니다: {preset}")
        rules_raw = PRESETS[preset]
    rules = []
    for i, r in enumerate(rules_raw):
        r = _require_dict(r, f"{where}.rules[{i}]")
        if "src" not in r or "dest" not in r:
            raise ConfigError(f"{where}.rules[{i}] 에 src, dest 가 필요합니다.")
        rules.append(Rule(
            src=_clean_prefix(str(r["src"])),
            dest=_clean_prefix(str(r["dest"] or "")),
            build_dir=_clean_prefix(str(r["build_dir"])) if r.get("build_dir") else None,
        ))

    modules = [_clean_prefix(str(m)) for m in (get("modules") or ["."])]
    encodings = get("source_encoding", ["utf-8", "cp949"])
    if isinstance(encodings, str):
        encodings = [encodings]

    package_name = str(get("package_name", DEFAULT_PACKAGE_NAME))
    sample = package_name.replace("{project}", name.replace("%", "%%"))
    try:
        rendered = datetime(2026, 1, 2, 3, 4, 5).strftime(sample)
    except ValueError as e:
        raise ConfigError(f"{where}.package_name 형식 오류: {e}") from e
    if not rendered.strip() or _UNSAFE_DIR_CHARS.search(rendered) or rendered.strip(".") == "":
        raise ConfigError(f"{where}.package_name 으로 만든 폴더 이름이 올바르지 않습니다: {rendered!r}")

    gp = get("gitlab_project")
    return ProjectConfig(
        name=name,
        root=_resolve(root, base_dir),
        output_dir=_resolve(output_dir, base_dir),
        server_path=server_path,
        gitlab_project=str(gp) if gp not in (None, "") else None,
        modules=modules,
        rules=rules,
        exclude=[str(x) for x in (get("exclude") or [])],
        source_encodings=[str(e) for e in encodings],
        package_name=package_name,
    )


def _clean_prefix(p: str) -> str:
    p = p.strip().replace("\\", "/").strip("/")
    return "" if p in ("", ".") else p


def _resolve(p: Any, base_dir: Path) -> Path:
    path = Path(os.path.expandvars(os.path.expanduser(str(p))))
    return path if path.is_absolute() else (base_dir / path).resolve()


def _opt_str(v: Any) -> Optional[str]:
    return None if v in (None, "") else str(v)


def _require_dict(v: Any, where: str) -> Dict[str, Any]:
    if not isinstance(v, dict):
        raise ConfigError(f"{where} 는 매핑(key: value)이어야 합니다.")
    return v
