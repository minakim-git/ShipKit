"""프로젝트 설정 파일(PROJECT_FILE) 만들기: 저장소를 보고 프리셋을 추정한다."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional, Tuple

from shipkit import CLI_NAME, CONFIG_NAME, __app_name__
from shipkit.config import PRESETS


def detect_preset(root: Path) -> Tuple[str, str]:
    """(프리셋, 판단 근거)를 돌려준다."""
    if (root / "pom.xml").is_file():
        return "maven", "pom.xml"
    for name in ("build.gradle", "build.gradle.kts"):
        if (root / name).is_file():
            return "gradle", name
    pkg = root / "package.json"
    if pkg.is_file():
        try:
            data = json.loads(pkg.read_text(encoding="utf-8"))
        except (ValueError, UnicodeDecodeError):
            data = {}
        deps = {**(data.get("dependencies") or {}), **(data.get("devDependencies") or {})}
        if "@vue/cli-service" in deps or (root / "vue.config.js").is_file():
            return "vue-cli", "package.json (@vue/cli-service)"
        if "vite" in deps or any((root / f"vite.config.{ext}").is_file() for ext in ("js", "ts", "mjs", "mts")):
            return "vite", "package.json (vite)"
    for name in ("manage.py", "requirements.txt", "pyproject.toml", "setup.py", "Pipfile"):
        if (root / name).is_file():
            return "python", name
    return "static", "빌드 도구를 찾지 못함"


def render(name: str, preset: str, server_path: Optional[str], gitlab_project: Optional[str]) -> str:
    lines = [
        f"# {__app_name__} 프로젝트 설정 - 팀이 공유하도록 저장소에 커밋하세요.",
        f"# PC 마다 다른 값(root, output_dir, 토큰)은 넣지 않습니다 -> 개인 설정({CONFIG_NAME})",
        f"# 프리셋 목록: {CLI_NAME} presets",
        "",
        f"name: {name}",
        f"preset: {preset}                      # {PRESETS[preset]['description']}",
    ]
    if server_path:
        lines.append(f"server_path: {server_path}")
    else:
        lines.append("# server_path: /app/tomcat/webapps/ROOT   # 필수: 서버 배포 경로를 적으세요")
    if gitlab_project:
        lines.append(f"gitlab_project: {gitlab_project}")
    else:
        lines.append('# gitlab_project: 123                     # GitLab 프로젝트 ID 또는 "그룹/프로젝트"')
    lines += [
        "",
        "# exclude: [\"docs/**\"]                     # 배포에서 뺄 파일 (프리셋 기본값에 추가)",
        "# hooks:                                   # 서버에서 실행할 명령 (실행 전에 확인을 받음)",
        "#   post_apply: [\"sudo systemctl restart tomcat\"]",
        "# server:",
        "#   owner: inherit                         # 새 파일·폴더 소유자를 상위 폴더와 같게",
    ]
    if PRESETS[preset]["mode"] == "sync":
        lines.insert(7, f"# build_dir: {PRESETS[preset]['build_dir']}                     # 빌드 결과 폴더")
        lines.insert(8, f"# prune: {PRESETS[preset]['prune']}            # 옛 빌드 파일을 정리할 서버 폴더")
    return "\n".join(lines) + "\n"


def find_repo_root(start: Path) -> Path:
    """git 저장소 최상위 폴더 (없으면 start)."""
    for d in [start] + list(start.parents):
        if (d / ".git").exists():
            return d
    return start
