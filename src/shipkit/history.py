"""만든 패키지의 기록: 패키지 폴더마다 기록 파일(shipkit-record.json)을 남기고, 목록으로 다시 읽는다.

기록 파일이 없는 예전 패키지는 report.md 와 manifest.txt 에서 읽을 수 있는 만큼만 채운다.
기록에는 '같은 내용으로 다시 만들기'에 필요한 변경 목록(git diff --name-status 형식)도 담는다.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional

from shipkit import __version__
from shipkit.config import ProjectConfig
from shipkit.models import Status

if TYPE_CHECKING:
    from shipkit.packager import PackageResult

RECORD_FILE = "shipkit-record.json"
RECORD_VERSION = 1
LIST_LIMIT = 300  # 화면에 보여 줄 최근 패키지 수
SCRIPTS = {"shipkit.sh": "linux", "shipkit.ps1": "windows"}
ARCHIVE_EXT = {"tar": ".tar.gz", "zip": ".zip"}
COUNT_KEYS = {"ok": Status.OK, "warn": Status.WARN, "error": Status.ERROR, "skipped": Status.SKIPPED}


@dataclass
class PackageRecord:
    path: Path
    created_at: datetime
    source_kind: str                      # gitlab-mr / github-pr / git-diff / manual / build / unknown
    description: str
    server_path: str = ""
    server_os: str = "linux"
    script_name: str = "shipkit.sh"
    applied: List[str] = field(default_factory=list)   # 서버에 반영할 파일
    deletes: List[str] = field(default_factory=list)   # 서버에서 지울 파일
    counts: Dict[str, int] = field(default_factory=dict)  # ok / warn / error / skipped (예전 패키지는 비어 있음)
    requests: List[dict] = field(default_factory=list)    # MR/PR: iid, ref, title, author, web_url
    changes: List[str] = field(default_factory=list)      # 다시 만들기용 'M<TAB>경로' 줄
    archive_kind: Optional[str] = None
    hooks: Dict[str, List[str]] = field(default_factory=dict)
    has_record: bool = True               # False = 기록 파일이 없는 예전 패키지

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def report(self) -> Path:
        return self.path / "report.md"

    @property
    def archive(self) -> Optional[Path]:
        if not self.archive_kind:
            return None
        candidate = self.path.with_name(self.path.name + ARCHIVE_EXT[self.archive_kind])
        return candidate if candidate.is_file() else None

    @property
    def request_kind(self) -> Optional[str]:
        """다시 만들 때 쓸 서비스 (gitlab / github)."""
        return {"gitlab-mr": "gitlab", "github-pr": "github"}.get(self.source_kind)

    def search_text(self) -> str:
        """목록 검색 대상: 설명, MR 번호·제목, 파일 경로."""
        parts = [self.name, self.description] + [f"{r.get('ref', '')} {r.get('title', '')}" for r in self.requests]
        return "\n".join(parts + self.applied + self.deletes).lower()


# ---------------------------------------------------------------------- 쓰기
def write_record(result: "PackageResult") -> Path:
    p, source = result.project, result.source
    changes = result.source_changes or [e.change for e in result.entries]
    data = {
        "version": RECORD_VERSION,
        "shipkit": __version__,
        "project": p.name,
        "created_at": result.created_at.isoformat(timespec="seconds"),
        "source": {
            "kind": source.kind,
            "description": source.description,
            "requests": [{k: mr.get(k, "") for k in ("iid", "ref", "title", "author", "web_url")}
                         for mr in source.merge_requests],
        },
        "server": {"path": p.server_path, "os": p.server_os, "script": p.script_name},
        "counts": {key: result.count(status) for key, status in COUNT_KEYS.items()},
        "applied": len(result.manifest),
        "deletes": len(result.deletes),
        "archive": result.archive_kind,
        "hooks": result.hooks,
        "changes": [f"{c.change.value}\t{c.path}" for c in changes],
    }
    path = result.out_dir / RECORD_FILE
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------- 읽기
def _lines(path: Path) -> List[str]:
    if not path.is_file():
        return []
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def is_package(folder: Path) -> bool:
    return folder.is_dir() and (folder / "manifest.txt").is_file() and any((folder / s).is_file() for s in SCRIPTS)


def read_record(folder: Path) -> Optional[PackageRecord]:
    if not is_package(folder):
        return None
    applied, deletes = _lines(folder / "manifest.txt"), _lines(folder / "delete.txt")
    meta_file = folder / RECORD_FILE
    if meta_file.is_file():
        try:
            data = json.loads(meta_file.read_text(encoding="utf-8"))
            source, server = data.get("source") or {}, data.get("server") or {}
            return PackageRecord(
                path=folder, created_at=datetime.fromisoformat(data["created_at"]),
                source_kind=source.get("kind", "unknown"), description=source.get("description", ""),
                server_path=server.get("path", ""), server_os=server.get("os", "linux"),
                script_name=server.get("script", "shipkit.sh"), applied=applied, deletes=deletes,
                counts=data.get("counts") or {}, requests=source.get("requests") or [],
                changes=data.get("changes") or [], archive_kind=data.get("archive"), hooks=data.get("hooks") or {},
            )
        except (ValueError, KeyError, TypeError):
            pass  # 손상된 기록 파일은 예전 패키지처럼 읽는다
    return _from_report(folder, applied, deletes)


def _from_report(folder: Path, applied: List[str], deletes: List[str]) -> PackageRecord:
    """기록 파일이 없는 예전 패키지: 보고서 머리말에서 시각·출처·서버 경로를 읽는다."""
    text = folder.joinpath("report.md").read_text(encoding="utf-8") if (folder / "report.md").is_file() else ""

    def field_of(label: str) -> str:
        m = re.search(rf"^- \*\*{label}\*\*: (.+)$", text, re.MULTILINE)
        return m.group(1).strip().strip("`") if m else ""

    try:
        created = datetime.strptime(field_of("생성 시각"), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        created = datetime.fromtimestamp((folder / "manifest.txt").stat().st_mtime)
    script_name = next(s for s in SCRIPTS if (folder / s).is_file())
    description = field_of("변경 출처") or "알 수 없음 (예전 패키지)"
    kind = "gitlab-mr" if description.startswith("GitLab MR") else (
        "github-pr" if description.startswith("GitHub PR") else "unknown")
    requests = [{"iid": int(n), "ref": f"{'!' if kind == 'gitlab-mr' else '#'}{n}", "title": ""}
                for n in re.findall(r"[!#](\d+)", description)] if kind != "unknown" else []
    archive = next((k for k, ext in ARCHIVE_EXT.items() if folder.with_name(folder.name + ext).is_file()), None)
    return PackageRecord(
        path=folder, created_at=created, source_kind=kind, description=description,
        server_path=field_of("서버 배포 경로"), server_os=SCRIPTS[script_name], script_name=script_name,
        applied=applied, deletes=deletes, requests=requests, archive_kind=archive, has_record=False,
    )


def list_records(project: ProjectConfig, limit: int = LIST_LIMIT) -> List[PackageRecord]:
    """프로젝트의 패키지 생성 경로에 있는 패키지를 최근 것부터."""
    base = project.output_dir
    if not base.is_dir():
        return []
    records = [r for r in (read_record(d) for d in base.iterdir()) if r is not None]
    records.sort(key=lambda r: (r.created_at, r.name), reverse=True)
    return records[:limit]
