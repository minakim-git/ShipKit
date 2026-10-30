"""매핑 계획(Entry 목록)을 실제 배포 패키지 폴더로 만든다.

패키지 구조:
  <output_dir>/<package_name, 기본 YYYY-MM-DD-HHMM>/   (같은 이름이 있으면 _2, _3 ...)
    files/          서버 배포 경로 기준 상대 경로 그대로의 파일들
    manifest.txt    반영할 파일 목록 (LF)
    delete.txt      서버에서 삭제할 파일 목록 (LF)
    prune.txt       (sync 모드) 이번 빌드에 없는 옛 파일을 정리할 서버 폴더 목록 (LF)
    hooks/          반영 전·후에 서버에서 실행할 명령 (pre_apply.sh 등, 설정한 경우만)
    checksums.sha256  위 파일들의 SHA-256 (서버에서 업로드 중 손상 여부 검증)
    <서버 스크립트>  check / backup / apply / rollback / history
                    리눅스: shipkit.sh (LF), Windows(server_os: windows): shipkit.ps1 (UTF-8 BOM, CRLF)
    report.md       배포 보고서
    shipkit-record.json  화면의 '기록'용 정보 (출처, 개수, 다시 만들기용 변경 목록)
"""
from __future__ import annotations

import hashlib
import shutil
import tarfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from shipkit import history, hooks, report, script
from shipkit.config import ProjectConfig
from shipkit.models import Entry, FileChange, SourceInfo, Status, normalize_repo_path


@dataclass
class PackageResult:
    project: ProjectConfig
    out_dir: Optional[Path]
    entries: List[Entry]
    source: SourceInfo
    created_at: datetime
    manifest: List[str]
    deletes: List[str]
    archive: Optional[Path] = None
    archive_kind: Optional[str] = None  # "tar" | "zip" (보고서 안내용)
    source_changes: List[FileChange] = field(default_factory=list)  # 수집한 소스 변경 (sync 모드 보고서용)
    hooks: Dict[str, List[str]] = field(default_factory=dict)        # 단계별 서버 실행 명령

    def count(self, status: Status) -> int:
        return sum(1 for e in self.entries if e.status == status)

    @property
    def has_errors(self) -> bool:
        return self.count(Status.ERROR) > 0


def resolve_conflicts(entries: List[Entry]) -> None:
    """서로 다른 변경이 같은 서버 파일을 만들면 뒤쪽 항목을 실패 처리한다."""
    owner: Dict[str, Entry] = {}
    for e in entries:
        for a in e.artifacts:
            other = owner.get(a.target)
            if other is not None and other is not e:
                e.fail(f"{a.target} 를 {other.change.path} 도 생성합니다 (대상 경로 충돌)")
            else:
                owner[a.target] = e
    for e in entries:
        if e.status == Status.ERROR:
            e.artifacts.clear()
            e.deletes.clear()


def collect_lists(entries: List[Entry]) -> Tuple[List[str], List[str]]:
    manifest = sorted({a.target for e in entries for a in e.artifacts})
    deletes = sorted({d for e in entries for d in e.deletes} - set(manifest))
    for p in manifest + deletes:
        normalize_repo_path(p)  # 서버 경로 밖을 가리키는 항목 방어
    return manifest, deletes


def build_package(project: ProjectConfig, entries: List[Entry], source: SourceInfo,
                  archive: Optional[str] = None, dry_run: bool = False,
                  now: Optional[datetime] = None,
                  source_changes: Optional[List[FileChange]] = None) -> PackageResult:
    created_at = now or datetime.now()
    resolve_conflicts(entries)
    manifest, deletes = collect_lists(entries)
    result = PackageResult(project, None, entries, source, created_at, manifest, deletes,
                           archive_kind=archive, source_changes=list(source_changes or []))
    result.hooks, hook_notes = hooks.select_hooks(project, result.source_changes)
    source.notes.extend(n for n in hook_notes if n not in source.notes)
    prev_name, changed = history.hooks_changed_since(project)
    if changed:  # 저장소의 .shipkit.yml 에서 누가 서버 명령을 바꿨으면 반영 전에 눈에 띄게
        msg = f"서버 명령(hooks)이 지난 패키지 {prev_name} 때와 다릅니다: " + " / ".join(changed)
        if msg not in source.warnings:
            source.warnings.append(msg)
    if dry_run:
        return result

    out_dir = _unique_dir(project.output_dir, project.render_package_name(created_at))
    files_dir = out_dir / "files"
    files_dir.mkdir(parents=True)
    for e in entries:
        for a in e.artifacts:
            dest = files_dir / a.target
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(a.local_file, dest)

    _write_lines(out_dir / "manifest.txt", manifest)
    _write_lines(out_dir / "delete.txt", deletes)
    _write_lines(out_dir / "prune.txt", project.prune if project.mode == "sync" else [])
    for stage, commands in result.hooks.items():
        (out_dir / "hooks").mkdir(exist_ok=True)
        if project.server_os == "windows":
            text, name = hooks.render_hook_file_ps(stage, commands), hooks.hook_file_name(stage, "windows")
            with open(out_dir / "hooks" / name, "w", encoding="utf-8-sig", newline="\r\n") as f:
                f.write(text)
        else:
            _write_text(out_dir / "hooks" / hooks.hook_file_name(stage, "linux"), hooks.render_hook_file(stage, commands))
    _write_checksums(out_dir, project.script_name)
    script.write_deploy_script(out_dir / project.script_name, project.server_path, out_dir.name, created_at,
                               project.server, project.server_os)
    result.out_dir = out_dir
    report.write_report(out_dir / "report.md", result)
    history.write_record(result)

    if archive:
        result.archive = _make_archive(out_dir, archive, project.script_name)
    return result


def _unique_dir(parent: Path, name: str) -> Path:
    candidate = parent / name
    n = 2
    while candidate.exists():
        candidate = parent / f"{name}_{n}"
        n += 1
    return candidate


def _write_text(path: Path, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def _write_checksums(out_dir: Path, script_name: str) -> None:
    """sha256sum -c 형식. 서버 스크립트 자신과 보고서는 제외한다."""
    lines = []
    candidates = [p for p in out_dir.rglob("*") if p.is_file()]
    for path in sorted(candidates, key=lambda p: p.relative_to(out_dir).as_posix()):
        rel = path.relative_to(out_dir).as_posix()
        if rel in (script_name, "report.md", "checksums.sha256", history.RECORD_FILE):
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        lines.append(f"{digest}  {rel}")
    _write_lines(out_dir / "checksums.sha256", lines)


def _write_lines(path: Path, lines: List[str]) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for line in lines:
            f.write(line + "\n")


def _make_archive(out_dir: Path, kind: str, script_name: str) -> Path:
    if kind == "zip":
        return Path(shutil.make_archive(str(out_dir), "zip", out_dir.parent, out_dir.name))
    if kind == "tar":
        target = out_dir.with_name(out_dir.name + ".tar.gz")

        def set_mode(info: tarfile.TarInfo) -> tarfile.TarInfo:
            # 윈도우에서 만든 tar 도 서버에서 바로 실행 가능하도록 권한을 지정한다.
            if info.isdir() or info.name.endswith("/" + script_name):
                info.mode = 0o755
            else:
                info.mode = 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            return info

        with tarfile.open(target, "w:gz", format=tarfile.PAX_FORMAT) as tar:
            tar.add(out_dir, arcname=out_dir.name, filter=set_mode)
        return target
    raise ValueError(f"지원하지 않는 압축 형식: {kind}")
