"""반영 전·후에 서버에서 실행할 명령(hooks) 고르기.

when 조건이 있는 명령은 그 패턴에 맞는 파일이 바뀐 경우에만 패키지에 넣는다.
(예: requirements.txt 가 바뀐 경우에만 pip install)
"""
from __future__ import annotations

from typing import Dict, List, Tuple

from shipkit.config import HOOK_STAGES, ProjectConfig, glob_match
from shipkit.models import FileChange


def select_hooks(project: ProjectConfig, changes: List[FileChange]) -> Tuple[Dict[str, List[str]], List[str]]:
    """단계별로 패키지에 넣을 명령과, 조건 때문에 빠진 명령에 대한 알림을 돌려준다."""
    selected: Dict[str, List[str]] = {}
    notes: List[str] = []
    paths = [c.path for c in changes]
    for stage in HOOK_STAGES:
        for hook in project.hooks.get(stage, []):
            if hook.when and paths and not any(glob_match(p, hook.when) for p in paths):
                notes.append(f"{stage} 명령 제외 ({hook.when} 변경 없음): {hook.run}")
                continue
            if hook.when and not paths:
                notes.append(f"변경 목록이 없어 조건부 {stage} 명령도 포함했습니다: {hook.run}")
            selected.setdefault(stage, []).append(hook.run)
    return selected, notes


def hook_file_name(stage: str, server_os: str) -> str:
    return f"{stage}.ps1" if server_os == "windows" else f"{stage}.sh"


def render_hook_file_ps(stage: str, commands: List[str]) -> str:
    """Windows 서버용. 명령마다 실패(외부 프로그램 종료 코드 포함)를 확인해서 바로 멈춘다.
    '#@' 로 끝나는 줄은 자동으로 붙인 줄이라 서버 스크립트가 화면에 보여주지 않는다."""
    lines = [
        f"# {stage}: 서버 배포 경로에서 실행됩니다. 명령 하나라도 실패하면 즉시 중단합니다.",
        "$ErrorActionPreference = 'Stop'  #@",
        "",
    ]
    for cmd in commands:
        lines.append("$global:LASTEXITCODE = 0  #@")
        lines.append(cmd.replace("\r\n", "\n").rstrip())
        lines.append("if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }  #@")
    return "\n".join(lines) + "\n"


def render_hook_file(stage: str, commands: List[str]) -> str:
    lines = [
        "#!/usr/bin/env bash",
        f"# {stage}: 서버 배포 경로에서 실행됩니다. 명령 하나라도 실패하면 즉시 중단합니다.",
        "set -e",
        "",
    ]
    for cmd in commands:
        lines.append(cmd.replace("\r\n", "\n").rstrip())
    return "\n".join(lines) + "\n"
