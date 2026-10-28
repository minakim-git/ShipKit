"""매핑 계획(Entry 목록)을 실제 배포 패키지 폴더로 만든다.

패키지 구조:
  <output_dir>/<package_name, 기본 YYYY-MM-DD-HHMM>/   (같은 이름이 있으면 _2, _3 ...)
    files/          서버 배포 경로 기준 상대 경로 그대로의 파일들
    manifest.txt    반영할 파일 목록 (LF)
    delete.txt      서버에서 삭제할 파일 목록 (LF)
    shipkit.sh       backup / apply / rollback / check (LF)
    report.md       배포 보고서
"""
from __future__ import annotations

import shutil
import tarfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from shipkit import report, script
from shipkit.config import ProjectConfig
from shipkit.models import Entry, SourceInfo, Status, normalize_repo_path


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
                  now: Optional[datetime] = None) -> PackageResult:
    created_at = now or datetime.now()
    resolve_conflicts(entries)
    manifest, deletes = collect_lists(entries)
    result = PackageResult(project, None, entries, source, created_at, manifest, deletes, archive_kind=archive)
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
    script.write_deploy_script(out_dir / "shipkit.sh", project.server_path, out_dir.name, created_at)
    result.out_dir = out_dir
    report.write_report(out_dir / "report.md", result)

    if archive:
        result.archive = _make_archive(out_dir, archive)
    return result


def _unique_dir(parent: Path, name: str) -> Path:
    candidate = parent / name
    n = 2
    while candidate.exists():
        candidate = parent / f"{name}_{n}"
        n += 1
    return candidate


def _write_lines(path: Path, lines: List[str]) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for line in lines:
            f.write(line + "\n")


def _make_archive(out_dir: Path, kind: str) -> Path:
    if kind == "zip":
        return Path(shutil.make_archive(str(out_dir), "zip", out_dir.parent, out_dir.name))
    if kind == "tar":
        target = out_dir.with_name(out_dir.name + ".tar.gz")

        def set_mode(info: tarfile.TarInfo) -> tarfile.TarInfo:
            # 윈도우에서 만든 tar 도 서버에서 바로 실행 가능하도록 권한을 지정한다.
            if info.isdir() or info.name.endswith("/shipkit.sh"):
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
