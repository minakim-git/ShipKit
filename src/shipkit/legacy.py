"""기존 배포 툴(compiled-java-deployer) DB 에서 프로젝트 설정을 가져와 shipkit.yaml 로 변환한다.

토큰·서버 비밀번호 같은 비밀 값은 읽지 않는다 (SELECT 대상 컬럼에서 제외).
"""
from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from shipkit import CLI_NAME, __app_name__


@dataclass
class LegacyProject:
    name: str
    local_path: str
    package_path: str
    server_path: str
    gitlab_project_id: str


def legacy_db_path() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / "compiled-java-deployer" / "database.db"


def read_legacy(db: Path) -> Tuple[Optional[str], List[LegacyProject]]:
    if not db.is_file():
        raise FileNotFoundError(f"기존 툴 DB 가 없습니다: {db}")
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT name, local_path, package_path, server_path, gitlab_project_id FROM projects ORDER BY id"
        ).fetchall()
        url_row = con.execute("SELECT value FROM settings WHERE key = 'gitlab_url'").fetchone()
    finally:
        con.close()
    projects = [LegacyProject(*(str(v or "").strip() for v in row)) for row in rows]
    return (url_row[0].strip() if url_row and url_row[0] else None), projects


def _q(value: str) -> str:
    # JSON 문자열은 YAML 큰따옴표 스칼라로도 유효하다.
    return json.dumps(value, ensure_ascii=False)


def _path(value: str) -> str:
    return value.replace("\\", "/").rstrip("/")


def to_yaml(gitlab_url: Optional[str], projects: List[LegacyProject], source: Path) -> str:
    out = [
        f"# {__app_name__} 설정 파일",
        f"# 기존 툴 DB({source})에서 가져왔습니다. 토큰/비밀번호는 가져오지 않습니다.",
        f"# 토큰은 '{CLI_NAME} token set' 으로 OS 키링에 저장하세요.",
        "",
    ]
    if gitlab_url:
        out += [
            "gitlab:",
            f"  url: {_q(gitlab_url.rstrip('/'))}",
            "  verify_ssl: true                  # 사내 인증서 오류 시 ca_bundle 지정",
            "  # ca_bundle: D:/certs/company-ca.pem",
            "  timeout: 15",
            "",
        ]
    out += [
        "defaults:",
        "  preset: maven",
        '  package_name: "%Y-%m-%d-%H%M"      # 기존 툴과 같은 날짜 폴더 이름',
        "  exclude:",
        '    - "src/test/**"',
        '    - "**/*.md"',
        "",
        "projects:",
    ]
    for p in projects:
        missing = [k for k, v in (("local_path", p.local_path), ("package_path", p.package_path),
                                  ("server_path", p.server_path)) if not v]
        prefix = "  # " if missing else "  "
        if missing:
            out.append(f"  # [확인 필요] {', '.join(missing)} 값이 없어 주석 처리했습니다.")
        out.append(f"{prefix}{_q(p.name)}:")
        out.append(f"{prefix}  root: {_q(_path(p.local_path))}")
        out.append(f"{prefix}  output_dir: {_q(_path(p.package_path))}")
        out.append(f"{prefix}  server_path: {_q(p.server_path.rstrip('/') or p.server_path)}")
        if p.gitlab_project_id:
            out.append(f"{prefix}  gitlab_project: {_q(p.gitlab_project_id)}")
        out.append("")
    return "\n".join(out)
