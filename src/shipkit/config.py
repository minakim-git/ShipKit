"""YAML 설정 파일 로더.

설정 파일 탐색 순서: --config 인자 > 환경변수(CONFIG_ENV) > 현재 폴더의 CONFIG_NAME > 사용자 공용 설정

설정은 두 군데에 나뉜다.
- 개인 설정(CONFIG_NAME): 로컬 경로(root), 패키지 생성 경로(output_dir), GitLab 주소 등 PC 마다 다른 값
- 프로젝트 설정(PROJECT_FILE): 프로젝트 저장소 루트에 커밋해서 팀이 공유하는 값 (프리셋, 서버 경로, hooks 등)
값의 우선순위는 개인 defaults < 프로젝트 설정 < 개인 설정의 프로젝트 항목. 목록형 값(exclude 등)은 합친다.

배포 방식(mode)
- changes: 변경 파일을 규칙(rules)에 따라 서버 경로로 매핑한다. (Java, Python, PHP, 정적 파일 등)
- sync   : 빌드 결과 폴더(build_dir) 전체를 패키지에 담고, 서버에서 다른 파일만 반영한다.
           결과 파일 이름이 빌드마다 바뀌는 프로젝트(Vue/React 등)용. prune 폴더의 옛 파일은 정리한다.
"""
from __future__ import annotations

import fnmatch
import os
import posixpath
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from shipkit import CLI_NAME, CONFIG_ENV, CONFIG_NAME, PROJECT_FILE

DEFAULT_CONFIG_NAME = CONFIG_NAME
# 패키지 폴더 이름 (strftime 형식, {project} = 프로젝트 이름). 기존 툴과 같은 2026-10-26-1133 형태.
DEFAULT_PACKAGE_NAME = "%Y-%m-%d-%H%M"
_UNSAFE_DIR_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
# 서버 스크립트에 그대로 들어가므로 셸이 해석할 수 있는 문자는 허용하지 않는다.
_UNSAFE_SHELL_CHARS = re.compile(r'["\'$`\\\n\r]')

MODES = ("changes", "sync")

# ---------------------------------------------------------------------- 프리셋
_COMMON_EXCLUDE = [
    ".git/**", ".gitignore", ".gitattributes", ".github/**", ".gitlab-ci.yml",
    ".idea/**", ".vscode/**", "**/.DS_Store", "**/Thumbs.db", "**/*.md",
]
_PYTHON_EXCLUDE = [
    "**/__pycache__/**", "**/*.pyc", ".venv/**", "venv/**", "env/**", "tests/**", "test/**",
    ".pytest_cache/**", ".tox/**", ".mypy_cache/**", "*.egg-info/**", "build/**", "dist/**",
]
_SQL_NOTICE = {"pattern": "**/*.sql", "message": "SQL 파일 변경: DB 반영이 따로 필요할 수 있습니다"}
_NODE_NOTICES = [
    {"pattern": "package.json", "message": "package.json 변경: 빌드 전에 npm install 을 했는지 확인하세요"},
    {"pattern": "package-lock.json", "message": "의존성 잠금 파일 변경: 빌드 전에 npm install(ci) 을 했는지 확인하세요"},
    {"pattern": "yarn.lock", "message": "의존성 잠금 파일 변경: 빌드 전에 yarn install 을 했는지 확인하세요"},
    {"pattern": "pnpm-lock.yaml", "message": "의존성 잠금 파일 변경: 빌드 전에 pnpm install 을 했는지 확인하세요"},
    {"pattern": ".env.production", "message": "운영 환경변수 파일 변경: 빌드 결과에 반영됐는지 확인하세요"},
]

PRESETS: Dict[str, Dict[str, Any]] = {
    "maven": {
        "description": "Java 웹 (Maven, exploded WAR): .java -> target/classes 의 .class",
        "mode": "changes",
        "rules": [
            {"src": "src/main/java", "dest": "WEB-INF/classes", "build_dir": "target/classes"},
            {"src": "src/main/resources", "dest": "WEB-INF/classes"},
            {"src": "src/main/webapp", "dest": ""},
        ],
        "notices": [
            {"pattern": "**/pom.xml", "message": "pom.xml 변경: 새 라이브러리는 WEB-INF/lib 에 따로 반영해야 할 수 있습니다"},
            _SQL_NOTICE,
        ],
    },
    "gradle": {
        "description": "Java 웹 (Gradle, exploded WAR): .java -> build/classes/java/main 의 .class",
        "mode": "changes",
        "rules": [
            {"src": "src/main/java", "dest": "WEB-INF/classes", "build_dir": "build/classes/java/main"},
            {"src": "src/main/resources", "dest": "WEB-INF/classes"},
            {"src": "src/main/webapp", "dest": ""},
        ],
        "notices": [
            {"pattern": "**/build.gradle*", "message": "build.gradle 변경: 새 라이브러리는 WEB-INF/lib 에 따로 반영해야 할 수 있습니다"},
            _SQL_NOTICE,
        ],
    },
    "python": {
        "description": "Python (Django/Flask/FastAPI 등): 바뀐 소스를 그대로 반영",
        "mode": "changes",
        "rules": [{"src": "", "dest": ""}],
        "exclude": _PYTHON_EXCLUDE,
        "notices": [
            {"pattern": "**/requirements*.txt", "message": "의존성 변경: 서버에서 pip install -r requirements.txt 가 필요합니다"},
            {"pattern": "pyproject.toml", "message": "pyproject.toml 변경: 서버에서 의존성 설치가 필요할 수 있습니다"},
            {"pattern": "Pipfile*", "message": "Pipfile 변경: 서버에서 pipenv install 이 필요합니다"},
            {"pattern": "poetry.lock", "message": "poetry.lock 변경: 서버에서 poetry install 이 필요합니다"},
            {"pattern": "**/migrations/*.py", "message": "DB 마이그레이션 변경: 서버에서 migrate 가 필요합니다"},
            _SQL_NOTICE,
        ],
    },
    "static": {
        "description": "정적 사이트·PHP·Node 서버 등: 바뀐 파일을 그대로 반영",
        "mode": "changes",
        "rules": [{"src": "", "dest": ""}],
        "exclude": ["node_modules/**"],
        "notices": [_SQL_NOTICE] + _NODE_NOTICES[:1],
    },
    "vite": {
        "description": "Vite 빌드 (Vue 3 / React 등): dist/ 전체 동기화, assets/ 옛 파일 정리",
        "mode": "sync",
        "build_dir": "dist",
        "prune": ["assets"],
        "notices": _NODE_NOTICES,
    },
    "vue-cli": {
        "description": "Vue CLI 빌드 (Vue 2/3): dist/ 전체 동기화, js/ css/ img/ fonts/ 옛 파일 정리",
        "mode": "sync",
        "build_dir": "dist",
        "prune": ["js", "css", "img", "fonts", "media"],
        "notices": _NODE_NOTICES,
    },
}
PRESET_ALIASES = {"vue-vite": "vite", "react-vite": "vite", "vue": "vite", "php": "static", "node": "static"}

# 실수로 배포되면 안 되는 파일 (allow 로 예외 지정 가능)
DEFAULT_DENY = [
    "**/.env", "**/.env.*", "**/*.pem", "**/*.key", "**/*.p12", "**/*.pfx", "**/*.jks", "**/*.keystore",
    "**/id_rsa", "**/id_rsa.*", "**/id_ed25519", "**/id_ed25519.*", "**/*.kdbx", "**/.npmrc", "**/.pypirc",
]
DEFAULT_ALLOW = ["**/.env.example", "**/.env.sample", "**/.env.template", "**/*.pub"]


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Rule:
    src: str               # 모듈 기준 소스 경로 접두어 (예: src/main/java, "" = 저장소 전체)
    dest: str              # 서버 배포 경로 기준 대상 접두어 (예: WEB-INF/classes, "" = 루트)
    build_dir: Optional[str] = None  # 지정 시 소스 대신 빌드 산출물을 복사 (.java -> .class)


@dataclass(frozen=True)
class Notice:
    pattern: str
    message: str


HOOK_STAGES = ("pre_apply", "post_apply", "post_rollback")
_OWNER_RE = re.compile(r"^[A-Za-z0-9._-]+(:[A-Za-z0-9._-]+)?$")
_MODE_RE = re.compile(r"^[0-7]{3,4}$")
DEFAULT_HISTORY_FILE = f"~/.{CLI_NAME}/history.log"


@dataclass(frozen=True)
class Hook:
    """서버에서 실행할 명령. when 이 있으면 그 패턴의 파일이 바뀐 경우에만 패키지에 넣는다."""
    run: str
    when: Optional[str] = None


@dataclass
class ServerOptions:
    owner: Optional[str] = None       # 새로 만든 파일·폴더의 소유자 ("user:group" 또는 "inherit")
    file_mode: Optional[str] = None   # 새로 만든 파일 권한 (예: 644)
    dir_mode: Optional[str] = None    # 새로 만든 폴더 권한 (예: 755)
    history_file: str = DEFAULT_HISTORY_FILE


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
    preset: Optional[str] = None
    mode: str = "changes"
    modules: List[str] = field(default_factory=lambda: [""])
    rules: List[Rule] = field(default_factory=list)
    build_dir: Optional[str] = None      # sync 모드: 빌드 결과 폴더 (root 기준)
    dest: str = ""                       # sync 모드: 서버 배포 경로 아래 반영 위치
    prune: List[str] = field(default_factory=list)  # sync 모드: 옛 파일을 정리할 서버 폴더
    exclude: List[str] = field(default_factory=list)
    notices: List[Notice] = field(default_factory=list)
    deny: List[str] = field(default_factory=lambda: list(DEFAULT_DENY))
    allow: List[str] = field(default_factory=lambda: list(DEFAULT_ALLOW))
    source_encodings: List[str] = field(default_factory=lambda: ["utf-8", "cp949"])
    package_name: str = DEFAULT_PACKAGE_NAME
    hooks: Dict[str, List[Hook]] = field(default_factory=dict)
    server: ServerOptions = field(default_factory=ServerOptions)
    project_file: Optional[Path] = None   # 읽어 들인 프로젝트 설정 파일 (없으면 None)
    discovered: bool = False              # 개인 설정에 없고 프로젝트 설정 파일만으로 찾은 프로젝트

    def is_excluded(self, path: str) -> bool:
        return any(glob_match(path, pat) for pat in self.exclude)

    def is_sensitive(self, path: str) -> bool:
        return any(glob_match(path, p) for p in self.deny) and not any(glob_match(path, p) for p in self.allow)

    def notices_for(self, path: str) -> List[str]:
        return [n.message for n in self.notices if glob_match(path, n.pattern)]

    def render_package_name(self, when: datetime) -> str:
        return when.strftime(self.package_name.replace("{project}", self.name.replace("%", "%%")))

    @property
    def describe_mode(self) -> str:
        preset = f"preset {self.preset}" if self.preset else "직접 규칙"
        if self.mode == "sync":
            dest = self.dest or "/"
            prune = ", ".join(self.prune) if self.prune else "없음"
            return f"빌드 결과 동기화 ({preset}: {self.build_dir} -> {dest}, 정리 폴더: {prune})"
        return f"변경 파일 매핑 ({preset})"


@dataclass
class Config:
    path: Optional[Path]
    gitlab: Optional[GitLabConfig]
    projects: Dict[str, ProjectConfig]
    defaults: Dict[str, Any] = field(default_factory=dict)
    base_dir: Path = field(default_factory=Path.cwd)

    def project(self, name: Optional[str], cwd: Optional[Path] = None) -> ProjectConfig:
        """이름으로 찾고, 이름이 없으면 현재 폴더로 추정한다.

        현재 폴더가 등록된 프로젝트 안이면 그 프로젝트, 아니면 상위 폴더의 프로젝트 설정 파일로 찾은 프로젝트,
        그것도 없고 등록된 프로젝트가 하나뿐이면 그 프로젝트.
        """
        if name is not None:
            if name not in self.projects:
                raise ConfigError(
                    f"설정에 없는 프로젝트입니다: {name}. 등록된 프로젝트: " + (", ".join(sorted(self.projects)) or "없음")
                )
            return self.projects[name]
        cwd = (cwd or Path.cwd()).resolve()
        inside = [p for p in self.projects.values() if _is_within(cwd, p.root)]
        if inside:
            return max(inside, key=lambda p: len(p.root.parts))
        found = self.discover(cwd)
        if found is not None:
            return found
        if len(self.projects) == 1:
            return next(iter(self.projects.values()))
        raise ConfigError(
            "프로젝트를 지정해주세요 (-p), 또는 프로젝트 폴더 안에서 실행하세요. 등록된 프로젝트: "
            + (", ".join(sorted(self.projects)) or "없음")
        )

    def discover(self, cwd: Path) -> Optional[ProjectConfig]:
        """개인 설정에 없는 저장소라도 프로젝트 설정 파일이 있으면 프로젝트로 쓴다."""
        for d in [cwd] + list(cwd.parents):
            if (d / PROJECT_FILE).is_file():
                shared = load_project_file(d / PROJECT_FILE)
                name = str(shared.get("name") or d.name)
                if "output_dir" not in self.defaults:
                    raise ConfigError(
                        f"{d / PROJECT_FILE} 를 찾았지만 개인 설정에 defaults.output_dir 가 없습니다. "
                        "예: output_dir: D:/deploy/{project}"
                    )
                project = _parse_project(name, {"root": str(d)}, self.defaults, self.base_dir)
                project.discovered = True
                return project
        return None


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root.resolve())
        return True
    except ValueError:
        return False


# 프로젝트 설정 파일에 넣으면 안 되는 개인 값
_PERSONAL_KEYS = ("root", "output_dir", "token")


def load_project_file(path: Path) -> Dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"프로젝트 설정 파일 YAML 문법 오류 ({path}): {e}") from e
    data = _require_dict(data, str(path))
    personal = [k for k in _PERSONAL_KEYS if k in data]
    if personal:
        raise ConfigError(
            f"{path} 에는 개인 설정({', '.join(personal)})을 넣을 수 없습니다. "
            f"PC 마다 다른 값은 개인 설정({CONFIG_NAME})에 적으세요."
        )
    return data


def glob_match(path: str, pattern: str) -> bool:
    """fnmatch 기반 매칭. '**/x' 는 루트의 'x' 도 매칭한다."""
    if fnmatch.fnmatchcase(path, pattern):
        return True
    return pattern.startswith("**/") and fnmatch.fnmatchcase(path, pattern[3:])


def user_config_path() -> Path:
    """사용자 공용 설정 위치: Windows 는 %APPDATA%\\<CLI_NAME>\\<CONFIG_NAME>"""
    base = os.environ.get("APPDATA") if os.name == "nt" else None
    if base:
        return Path(base) / CLI_NAME / CONFIG_NAME
    return Path.home() / ".config" / CLI_NAME / CONFIG_NAME


def find_config(explicit: Optional[str]) -> Path:
    """설정 파일 탐색: --config > 환경변수 > ./설정파일 > 사용자 공용 설정"""
    if explicit:
        p = Path(explicit)
        if not p.is_file():
            raise ConfigError(f"설정 파일을 찾을 수 없습니다: {p}")
        return p
    env = os.environ.get(CONFIG_ENV)
    if env:
        p = Path(env)
        if not p.is_file():
            raise ConfigError(f"{CONFIG_ENV} 가 가리키는 설정 파일이 없습니다: {p}")
        return p
    for p in (Path.cwd() / CONFIG_NAME, user_config_path()):
        if p.is_file():
            return p
    raise ConfigError(
        f"설정 파일이 없습니다. '{CLI_NAME} init' 으로 {user_config_path()} 를 만들거나 --config 로 지정하세요."
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
                f"환경변수(gitlab.token_env) 또는 '{CLI_NAME} token set' 을 사용하세요."
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

    projects = {}
    for name, raw in raw_projects.items():
        projects[str(name)] = _parse_project(str(name), _require_dict(raw, f"projects.{name}"), defaults, base_dir)
    return Config(path=path, gitlab=gitlab, projects=projects, defaults=defaults, base_dir=base_dir)


def resolve_preset(name: str, where: str) -> Dict[str, Any]:
    key = PRESET_ALIASES.get(name, name)
    if key not in PRESETS:
        names = ", ".join(list(PRESETS) + list(PRESET_ALIASES))
        raise ConfigError(f"{where}.preset 은 다음 중 하나여야 합니다: {names} (입력값: {name})")
    return PRESETS[key]


def _parse_project(name: str, raw: Dict[str, Any], defaults: Dict[str, Any], base_dir: Path) -> ProjectConfig:
    where = f"projects.{name}"
    root_value = raw.get("root") or defaults.get("root")
    if not root_value:
        raise ConfigError(f"{where}.root (프로젝트 로컬 경로)가 필요합니다.")
    root = _resolve(root_value, base_dir)
    project_file = root / PROJECT_FILE
    shared = load_project_file(project_file) if project_file.is_file() else {}
    layers = [defaults, shared, raw]  # 뒤가 우선

    def get(key: str, default: Any = None) -> Any:
        for layer in reversed(layers):
            if key in layer:
                return layer[key]
        return default

    def get_list(key: str) -> List[Any]:
        merged: List[Any] = []
        for layer in layers:
            merged.extend(layer.get(key) or [])
        return merged

    output_dir = raw.get("output_dir") or defaults.get("output_dir")
    if not output_dir:
        raise ConfigError(f"{where}.output_dir (패키지 생성 경로)가 필요합니다.")
    output_dir = str(output_dir).replace("{project}", name)
    server_path = _parse_server_path(get("server_path"), where)

    # 직접 규칙(rules)만 주고 preset 을 안 주면 프리셋 없이 changes 모드로 동작한다.
    preset_name = get("preset")
    if preset_name is None and not get("rules") and not get("build_dir"):
        preset_name = "maven"  # 0.1 설정 호환
    preset = resolve_preset(str(preset_name), where) if preset_name else {}
    canonical = PRESET_ALIASES.get(str(preset_name), str(preset_name)) if preset_name else None

    mode = str(get("mode", preset.get("mode", "sync" if get("build_dir") else "changes")))
    if mode not in MODES:
        raise ConfigError(f"{where}.mode 는 {', '.join(MODES)} 중 하나여야 합니다: {mode}")

    rules: List[Rule] = []
    build_dir = dest = None
    prune: List[str] = []
    if mode == "changes":
        rules_raw = get("rules") or preset.get("rules")
        if not rules_raw:
            raise ConfigError(f"{where}: changes 모드에는 preset 또는 rules 가 필요합니다.")
        for i, r in enumerate(rules_raw):
            r = _require_dict(r, f"{where}.rules[{i}]")
            if "src" not in r or "dest" not in r:
                raise ConfigError(f"{where}.rules[{i}] 에 src, dest 가 필요합니다.")
            rules.append(Rule(
                src=_clean_prefix(str(r["src"] or "")),
                dest=_clean_prefix(str(r["dest"] or "")),
                build_dir=_clean_prefix(str(r["build_dir"])) if r.get("build_dir") else None,
            ))
    else:
        build_dir = _clean_prefix(str(get("build_dir", preset.get("build_dir")) or ""))
        if not build_dir:
            raise ConfigError(f"{where}.build_dir (빌드 결과 폴더, 예: dist)가 필요합니다.")
        _check_relative(build_dir, f"{where}.build_dir")
        dest = _clean_prefix(str(get("dest", "") or ""))
        if dest:
            _check_relative(dest, f"{where}.dest")
        for p in get("prune", preset.get("prune", [])) or []:
            p = str(p).strip().replace("\\", "/")
            if p.startswith("/"):
                raise ConfigError(f"{where}.prune 은 server_path 기준 상대 경로여야 합니다: {p}")
            p = p.strip("/")
            if not p:
                raise ConfigError(f"{where}.prune 에 빈 값이 있습니다. 서버 경로 전체를 정리하려면 '.' 을 명시하세요.")
            if p != ".":
                _check_relative(p, f"{where}.prune")
            if _UNSAFE_SHELL_CHARS.search(p):
                raise ConfigError(f"{where}.prune 에 사용할 수 없는 문자가 있습니다: {p}")
            prune.append(p)

    modules = [_clean_prefix(str(m)) for m in (get("modules") or ["."])]
    encodings = get("source_encoding", ["utf-8", "cp949"])
    if isinstance(encodings, str):
        encodings = [encodings]

    notices = [Notice(**n) for n in preset.get("notices", [])]
    for i, n in enumerate(get_list("notices")):
        n = _require_dict(n, f"{where}.notices[{i}]")
        if not n.get("pattern") or not n.get("message"):
            raise ConfigError(f"{where}.notices[{i}] 에 pattern, message 가 필요합니다.")
        notices.append(Notice(str(n["pattern"]), str(n["message"])))

    exclude = list(_COMMON_EXCLUDE) + list(preset.get("exclude", [])) + [str(x) for x in get_list("exclude")]

    gp = get("gitlab_project")
    return ProjectConfig(
        name=name,
        root=root,
        output_dir=_resolve(output_dir, base_dir),
        server_path=server_path,
        gitlab_project=str(gp) if gp not in (None, "") else None,
        preset=canonical,
        mode=mode,
        modules=modules,
        rules=rules,
        build_dir=build_dir,
        dest=dest or "",
        prune=prune,
        exclude=list(dict.fromkeys(exclude)),
        notices=notices,
        deny=list(dict.fromkeys(list(DEFAULT_DENY) + [str(x) for x in get_list("deny")])),
        allow=list(dict.fromkeys(list(DEFAULT_ALLOW) + [str(x) for x in get_list("allow")])),
        source_encodings=[str(e) for e in encodings],
        package_name=_parse_package_name(str(get("package_name", DEFAULT_PACKAGE_NAME)), name, where),
        hooks=_parse_hooks(get("hooks"), where),
        server=_parse_server_options(get("server"), where),
        project_file=project_file if project_file.is_file() else None,
    )


def _parse_hooks(raw: Any, where: str) -> Dict[str, List[Hook]]:
    if not raw:
        return {}
    raw = _require_dict(raw, f"{where}.hooks")
    hooks: Dict[str, List[Hook]] = {}
    for stage, items in raw.items():
        if stage not in HOOK_STAGES:
            raise ConfigError(f"{where}.hooks 의 단계는 {', '.join(HOOK_STAGES)} 중 하나여야 합니다: {stage}")
        if isinstance(items, (str, dict)):
            items = [items]
        parsed = []
        for i, item in enumerate(items or []):
            if isinstance(item, str):
                item = {"run": item}
            item = _require_dict(item, f"{where}.hooks.{stage}[{i}]")
            run = str(item.get("run") or "").strip()
            if not run or "\0" in run:
                raise ConfigError(f"{where}.hooks.{stage}[{i}].run 이 비어 있습니다.")
            parsed.append(Hook(run=run, when=_opt_str(item.get("when"))))
        if parsed:
            hooks[stage] = parsed
    return hooks


def _parse_server_options(raw: Any, where: str) -> ServerOptions:
    if not raw:
        return ServerOptions()
    raw = _require_dict(raw, f"{where}.server")
    owner = _opt_str(raw.get("owner"))
    if owner and owner != "inherit" and not _OWNER_RE.match(owner):
        raise ConfigError(f"{where}.server.owner 는 'user', 'user:group' 또는 'inherit' 여야 합니다: {owner}")
    modes = {}
    for key in ("file_mode", "dir_mode"):
        value = _opt_str(raw.get(key))
        if value is not None:
            value = value.zfill(3) if value.isdigit() else value
            if not _MODE_RE.match(value):
                raise ConfigError(f"{where}.server.{key} 는 8진수 권한이어야 합니다 (예: 644): {value}")
        modes[key] = value
    history = str(raw.get("history_file") or DEFAULT_HISTORY_FILE).strip()
    if _UNSAFE_SHELL_CHARS.search(history) or not (history.startswith("/") or history.startswith("~/")):
        raise ConfigError(f"{where}.server.history_file 은 '/' 또는 '~/' 로 시작하고 따옴표·$ 등을 쓸 수 없습니다: {history}")
    return ServerOptions(owner=owner, file_mode=modes["file_mode"], dir_mode=modes["dir_mode"], history_file=history)


def _parse_server_path(value: Any, where: str) -> str:
    server_path = str(value or "").strip()
    if not server_path:
        raise ConfigError(f"{where}.server_path (서버 배포 경로, 예: /app/tomcat/webapps/ROOT)가 필요합니다.")
    if not server_path.startswith("/"):
        raise ConfigError(f"{where}.server_path 는 '/' 로 시작하는 리눅스 절대 경로여야 합니다: {server_path}")
    if _UNSAFE_SHELL_CHARS.search(server_path):
        raise ConfigError(f"{where}.server_path 에 사용할 수 없는 문자(\" ' $ ` \\)가 있습니다.")
    return server_path.rstrip("/") or "/"


def _parse_package_name(package_name: str, name: str, where: str) -> str:
    sample = package_name.replace("{project}", name.replace("%", "%%"))
    try:
        rendered = datetime(2026, 1, 2, 3, 4, 5).strftime(sample)
    except ValueError as e:
        raise ConfigError(f"{where}.package_name 형식 오류: {e}") from e
    if not rendered.strip() or _UNSAFE_DIR_CHARS.search(rendered) or rendered.strip(".") == "":
        raise ConfigError(f"{where}.package_name 으로 만든 폴더 이름이 올바르지 않습니다: {rendered!r}")
    return package_name


def _check_relative(p: str, where: str) -> None:
    norm = posixpath.normpath(p)
    if p.startswith("/") or (len(p) > 1 and p[1] == ":") or norm == ".." or norm.startswith("../"):
        raise ConfigError(f"{where} 는 상대 경로여야 하고 상위 폴더(..)를 가리킬 수 없습니다: {p}")


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
