"""변경 파일 -> 서버 배포 파일 매핑 (파일 복사 없이 계획만 만든다).

규칙(Rule)은 '모듈 기준 소스 접두어 -> 서버 배포 경로 기준 대상 접두어' 이다.
build_dir 이 있는 규칙의 .java 는 빌드 산출물(.class)로 바뀐다:
  X.java -> X.class + X$*.class(내부/익명 클래스) + 같은 파일에 선언된 다른 최상위 타입
"""
from __future__ import annotations

import posixpath
import re
from pathlib import Path
from typing import List, Optional, Tuple

from shipkit.config import ProjectConfig, Rule
from shipkit.models import Artifact, ChangeType, Entry, FileChange, Status

# 열 0에서 시작하는 최상위 타입 선언 (중첩 타입은 들여쓰기되어 있으므로 제외된다)
_TOP_LEVEL_TYPE = re.compile(
    r"^(?:(?:public|protected|private|abstract|final|static|sealed|non-sealed|strictfp)\s+)*"
    r"(?:class|interface|enum|record|@interface)\s+([A-Za-z_$][\w$]*)",
    re.MULTILINE,
)
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT = re.compile(r"//[^\n]*")
_STRING = re.compile(r'"(?:\\.|[^"\\\n])*"')

# 소스와 클래스의 수정 시각 차이 허용치(초). 파일 시스템 시각 정밀도 차이를 흡수한다.
STALE_TOLERANCE = 2.0


def plan(project: ProjectConfig, changes: List[FileChange]) -> List[Entry]:
    return [plan_one(project, ch) for ch in changes]


def plan_one(project: ProjectConfig, change: FileChange) -> Entry:
    entry = Entry(change)
    if project.is_excluded(change.path):
        entry.status = Status.SKIPPED
        entry.messages.append("제외 패턴에 해당")
        return entry

    matched = _match_rule(project, change.path)
    if matched is None:
        entry.status = Status.SKIPPED
        entry.messages.append("매핑 규칙 없음 (서버 배포 대상 아님)")
        return entry
    module, rule, sub = matched
    if not sub:
        entry.status = Status.SKIPPED
        entry.messages.append("규칙의 기준 폴더 자체는 배포 대상이 아님")
        return entry

    module_root = project.root / module if module else project.root
    target = posixpath.join(rule.dest, sub) if rule.dest else sub

    if rule.build_dir and change.path.endswith(".java"):
        _plan_java(project, entry, module_root, rule, sub)
    else:
        _plan_copy(entry, module_root, rule, sub, target)
    return entry


def _match_rule(project: ProjectConfig, path: str) -> Optional[Tuple[str, Rule, str]]:
    # 긴 모듈 접두어부터 매칭해서 'web' 과 'web-admin' 같은 모듈이 섞이지 않게 한다.
    for module in sorted(project.modules, key=len, reverse=True):
        prefix = module + "/" if module else ""
        if not path.startswith(prefix):
            continue
        rel = path[len(prefix):]
        for rule in project.rules:
            if not rule.src:
                return module, rule, rel
            if rel == rule.src or rel.startswith(rule.src + "/"):
                return module, rule, rel[len(rule.src):].lstrip("/")
    return None


def _plan_copy(entry: Entry, module_root: Path, rule: Rule, sub: str, target: str) -> None:
    if entry.change.change == ChangeType.DELETED:
        entry.deletes.append(target)
        return
    src_file = module_root / rule.src / sub
    if rule.build_dir:
        built = module_root / rule.build_dir / sub
        if built.is_file():
            entry.artifacts.append(Artifact(target, built))
            return
        if src_file.is_file():
            entry.artifacts.append(Artifact(target, src_file))
            entry.warn(f"빌드 결과({rule.build_dir})에 없어 소스 파일을 복사합니다")
            return
    elif src_file.is_file():
        entry.artifacts.append(Artifact(target, src_file))
        return
    entry.fail(f"로컬에 파일이 없습니다: {src_file} (git pull/브랜치 확인)")


def _plan_java(project: ProjectConfig, entry: Entry, module_root: Path, rule: Rule, sub: str) -> None:
    sub_dir, file_name = posixpath.split(sub)
    stem = file_name[: -len(".java")]
    class_dir = module_root / rule.build_dir / sub_dir if sub_dir else module_root / rule.build_dir
    dest_dir = posixpath.join(rule.dest, sub_dir) if sub_dir else rule.dest
    source_file = module_root / rule.src / sub

    def target_of(cls: Path) -> str:
        return posixpath.join(dest_dir, cls.name) if dest_dir else cls.name

    if entry.change.change == ChangeType.DELETED:
        entry.deletes.append(target_of(class_dir / f"{stem}.class"))
        for cls in _class_files(class_dir, stem, include_main=False):
            entry.deletes.append(target_of(cls))
        entry.messages.append(f"서버에 남은 {stem}$*.class 내부 클래스가 있으면 수동 삭제가 필요할 수 있습니다")
        return

    main_class = class_dir / f"{stem}.class"
    if not main_class.is_file():
        entry.fail(f"컴파일된 클래스가 없습니다: {main_class} (빌드 필요)")
        return

    type_names = [stem]
    if source_file.is_file():
        others, err = _secondary_types(source_file, stem, project.source_encodings)
        if err:
            entry.warn(err)
        type_names.extend(others)
    else:
        entry.warn(f"소스 파일이 로컬에 없어 빌드 누락 여부를 확인하지 못했습니다: {source_file}")

    classes: List[Path] = []
    for name in type_names:
        classes.extend(_class_files(class_dir, name, include_main=True))
    classes = sorted(dict.fromkeys(classes))
    for cls in classes:
        entry.artifacts.append(Artifact(target_of(cls), cls))

    if source_file.is_file():
        src_mtime = source_file.stat().st_mtime
        if main_class.stat().st_mtime + STALE_TOLERANCE < src_mtime:
            entry.warn("소스가 클래스 파일보다 최신입니다 - 빌드 누락 의심 (다시 빌드하세요)")


def _class_files(class_dir: Path, name: str, include_main: bool) -> List[Path]:
    if not class_dir.is_dir():
        return []
    found = []
    main = class_dir / f"{name}.class"
    if include_main and main.is_file():
        found.append(main)
    found.extend(sorted(p for p in class_dir.glob(f"{name}$*.class") if p.is_file()))
    return found


def _secondary_types(source_file: Path, stem: str, encodings: List[str]) -> Tuple[List[str], Optional[str]]:
    raw = source_file.read_bytes()
    text = None
    for enc in encodings:
        try:
            text = raw.decode(enc)
            break
        except (UnicodeDecodeError, LookupError):
            continue
    if text is None:
        return [], f"소스 인코딩을 판별하지 못해 보조 최상위 클래스 탐지를 건너뜁니다 ({', '.join(encodings)})"
    text = _STRING.sub('""', _LINE_COMMENT.sub("", _BLOCK_COMMENT.sub("", text)))
    names = [n for n in _TOP_LEVEL_TYPE.findall(text) if n != stem]
    return list(dict.fromkeys(names)), None
