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


def config_dict(project: ProjectConfig) -> Dict[str, List[dict]]:
    """설정된 서버 명령 전체(조건 포함)를 기록 파일에 남길 모양으로. 지난 패키지와 비교하는 데 쓴다."""
    return {stage: [{"run": h.run, "when": h.when or ""} for h in hooks]
            for stage, hooks in project.hooks.items() if hooks}


def describe_changes(previous: Dict[str, List[dict]], current: Dict[str, List[dict]]) -> List[str]:
    """지난 패키지의 서버 명령 설정과 지금 설정의 차이를 사람이 읽을 줄로. 같으면 빈 목록.

    저장소에 커밋된 .shipkit.yml 의 hooks 는 쓰기 권한이 있는 누구나 바꿀 수 있는 서버 명령이라,
    모르는 사이에 바뀐 것을 반영 전에 알아채도록 한다.
    """
    lines: List[str] = []
    for stage in HOOK_STAGES:
        before = {(h.get("run", ""), h.get("when") or "") for h in previous.get(stage, [])}
        after = {(h.get("run", ""), h.get("when") or "") for h in current.get(stage, [])}
        before_runs = {run for run, _ in before}
        after_runs = {run for run, _ in after}
        for run, when in sorted(after - before):
            if run in before_runs:
                lines.append(f"{stage} 조건 바뀜: {run}" + (f" (when: {when})" if when else " (조건 없음)"))
            else:
                lines.append(f"{stage} 추가: {run}" + (f" (when: {when})" if when else ""))
        for run, when in sorted(before - after):
            if run not in after_runs:
                lines.append(f"{stage} 빠짐: {run}")
    return lines


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
