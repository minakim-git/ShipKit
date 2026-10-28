"""ShipKit 명령줄 인터페이스.

  shipkit init                      설정 파일(shipkit.yaml) 생성
  shipkit import-legacy             기존 배포 툴(compiled-java-deployer) 프로젝트 설정 가져오기
  shipkit check   [-p 프로젝트]      설정·경로·GitLab 연결 점검
  shipkit mr      [-p 프로젝트]      GitLab MR 목록
  shipkit package [-p 프로젝트] --mr 12 15 | --git A..B | --files 목록.txt | 경로...
                                    (sync 모드 프로젝트는 변경 출처 없이도 실행 가능)
  shipkit presets                   사용 가능한 프리셋 목록
  shipkit token   set | delete | status

종료 코드: 0 성공, 1 처리 실패(또는 --strict 경고), 2 설정·사용법 오류
"""
from __future__ import annotations

import argparse
import getpass
import logging
import sqlite3
import sys
from importlib import resources
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from shipkit import (
    CLI_NAME, SCRIPT_NAME, __app_name__, __tagline__, __version__, legacy, mapper, packager, sync,
)
from shipkit.config import (
    DEFAULT_CONFIG_NAME, PRESET_ALIASES, PRESETS, Config, ConfigError, ProjectConfig, load_config,
    user_config_path,
)
from shipkit.models import Entry, FileChange, SourceInfo, Status, merge_changes
from shipkit.secrets import SecretError, delete_token, get_token, set_token, token_source
from shipkit.sources import git_local, manual
from shipkit.sources.gitlab import GitLabClient, GitLabError, collect_from_mrs

log = logging.getLogger(CLI_NAME)

EXIT_OK, EXIT_FAIL, EXIT_USAGE = 0, 1, 2

_TAG = {Status.OK: "[성공]", Status.WARN: "[경고]", Status.ERROR: "[실패]", Status.SKIPPED: "[제외]"}


class UsageError(Exception):
    pass


# ---------------------------------------------------------------------- 공통
def _client(cfg: Config) -> GitLabClient:
    if cfg.gitlab is None:
        raise UsageError("설정 파일에 gitlab 섹션이 없습니다.")
    return GitLabClient(cfg.gitlab, get_token(cfg.gitlab))


def _gitlab_project(project: ProjectConfig) -> str:
    if not project.gitlab_project:
        raise UsageError(f"projects.{project.name}.gitlab_project 가 설정되지 않았습니다.")
    return project.gitlab_project


# ---------------------------------------------------------------------- init
def _write_new_file(target: Path, text: str, force: bool) -> None:
    if target.exists() and not force:
        raise UsageError(f"{target} 가 이미 있습니다. 덮어쓰려면 --force 를 붙이세요.")
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def cmd_init(args: argparse.Namespace) -> int:
    target = Path(args.path) if args.path else user_config_path()
    text = (resources.files("shipkit") / "templates" / "config.example.yaml").read_text(encoding="utf-8")
    _write_new_file(target, text, args.force)
    print(f"설정 파일을 만들었습니다: {target.resolve()}")
    print(f"gitlab.url, projects 항목을 실제 값으로 고친 뒤 '{CLI_NAME} check' 로 점검하세요.")
    return EXIT_OK


def cmd_import_legacy(args: argparse.Namespace) -> int:
    db = Path(args.db) if args.db else legacy.legacy_db_path()
    try:
        gitlab_url, projects = legacy.read_legacy(db)
    except (FileNotFoundError, sqlite3.Error) as e:
        raise UsageError(f"기존 툴 DB 를 읽지 못했습니다: {e}") from e
    if not projects:
        raise UsageError(f"기존 툴 DB 에 등록된 프로젝트가 없습니다: {db}")
    text = legacy.to_yaml(gitlab_url, projects, db)
    target = Path(args.out) if args.out else user_config_path()
    _write_new_file(target, text, args.force)
    load_config(str(target))  # 만든 설정이 바로 읽히는지 확인
    print(f"기존 툴 프로젝트 {len(projects)}개를 가져왔습니다: {target.resolve()}")
    for p in projects:
        print(f"  - {p.name}  ->  서버 {p.server_path or '(없음)'}")
    print(f"토큰은 가져오지 않았습니다. '{CLI_NAME} token set' 후 '{CLI_NAME} check' 로 점검하세요.")
    return EXIT_OK


# ---------------------------------------------------------------------- check
def cmd_check(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    print(f"설정 파일: {cfg.path}")
    names = [args.project] if args.project else sorted(cfg.projects)
    ok = True

    def item(good: bool, msg: str) -> None:
        nonlocal ok
        ok &= good
        print(f"  {'[정상]' if good else '[문제]'} {msg}")

    for name in names:
        p = cfg.project(name)
        print(f"\n프로젝트 {p.name}")
        item(p.root.is_dir(), f"로컬 경로: {p.root}")
        if p.root.is_dir():
            item(git_local.is_git_repo(p.root), "git 저장소 여부 (--git 모드와 MR 반영 확인에 필요)")
            if p.mode == "sync":
                bd = sync.build_root(p)
                count = sum(1 for f in bd.rglob("*") if f.is_file()) if bd.is_dir() else 0
                item(count > 0, f"빌드 결과 폴더: {bd} ({count}개 파일)" if count
                     else f"빌드 결과 폴더가 없거나 비어 있음: {bd} (빌드 필요)")
            for module in p.modules:
                base = p.root / module if module else p.root
                for rule in p.rules:
                    if rule.build_dir:
                        bd = base / rule.build_dir
                        item(bd.is_dir(), f"빌드 결과 폴더: {bd}")
        out_parent = p.output_dir if p.output_dir.exists() else p.output_dir.parent
        item(out_parent.exists(), f"패키지 생성 경로: {p.output_dir}")
        print(f"  [정보] 서버 배포 경로: {p.server_path}")
        print(f"  [정보] 배포 방식: {p.describe_mode}")
        for stage, hooks in p.hooks.items():
            for h in hooks:
                cond = f" (조건: {h.when} 변경 시)" if h.when else ""
                print(f"  [정보] {stage} 명령: {h.run}{cond}")
        s = p.server
        if s.owner or s.file_mode or s.dir_mode:
            print(f"  [정보] 새 파일·폴더: 소유자 {s.owner or '그대로'} / 파일 {s.file_mode or '그대로'}"
                  f" / 폴더 {s.dir_mode or '그대로'}")
        if p.mode == "changes":
            print("  [정보] 매핑 규칙: " + ", ".join(f"{r.src or '(전체)'} -> {r.dest or '/'}" for r in p.rules))

    if cfg.gitlab:
        print(f"\nGitLab {cfg.gitlab.url}")
        src = token_source(cfg.gitlab)
        item(src is not None, f"토큰: {src or '없음'}")
        if src:
            try:
                client = _client(cfg)
                user = client.current_user()
                item(True, f"연결/인증: {user.get('username')} ({user.get('name')})")
                for name in names:
                    p = cfg.project(name)
                    if p.gitlab_project:
                        try:
                            info = client.get_project(p.gitlab_project)
                            item(True, f"{p.name} -> GitLab 프로젝트 {info.get('path_with_namespace')}")
                        except GitLabError as e:
                            item(False, f"{p.name} -> GitLab 프로젝트 {p.gitlab_project}: {e}")
            except (GitLabError, SecretError) as e:
                item(False, f"연결: {e}")
    print("\n점검 결과: " + ("이상 없음" if ok else "문제 있음"))
    return EXIT_OK if ok else EXIT_FAIL


# ---------------------------------------------------------------------- mr
def cmd_mr(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    project = cfg.project(args.project)
    client = _client(cfg)
    mrs = client.list_merge_requests(_gitlab_project(project), state=args.state,
                                     limit=args.limit, search=args.search)
    if not mrs:
        print("조회된 MR 이 없습니다.")
        return EXIT_OK
    print(f"{'MR':>6}  {'상태':<7} {'머지/수정 시각':<16}  {'작성자':<10}  제목")
    for mr in mrs:
        when = (mr.get("merged_at") or mr.get("updated_at") or "")[:16].replace("T", " ")
        author = (mr.get("author") or {}).get("name", "")
        print(f"{'!' + str(mr['iid']):>6}  {mr.get('state', ''):<7} {when:<16}  {author:<10}  {mr.get('title', '')}")
    return EXIT_OK


# ---------------------------------------------------------------------- package
def _collect(args: argparse.Namespace, cfg: Config, project: ProjectConfig) -> Tuple[List[FileChange], SourceInfo]:
    modes = sum(bool(x) for x in (args.mr, args.git, args.files, args.paths))
    if modes == 0 and project.mode == "sync":
        return [], SourceInfo(kind="build", description=f"빌드 결과 전체 ({project.build_dir}, 변경 목록 없음)")
    if modes != 1:
        raise UsageError("변경 파일 출처를 하나만 지정하세요: --mr, --git, --files, 또는 경로 직접 나열")

    if args.mr:
        changes, source = collect_from_mrs(_client(cfg), _gitlab_project(project), args.mr)
        _check_local_has_mrs(project, source)
        return changes, source

    if args.git:
        changes, desc = git_local.collect_from_git(project.root, args.git)
        source = SourceInfo(kind="git-diff", description=desc)
        _, head = git_local.parse_range(args.git)
        head_sha = git_local.resolve_commit(project.root, head)
        local_sha = git_local.resolve_commit(project.root, "HEAD")
        if head_sha and local_sha and head_sha != local_sha:
            source.warnings.append(
                f"로컬 HEAD({local_sha[:9]})가 범위 끝 {head}({head_sha[:9]})와 다릅니다. "
                "복사되는 파일은 지금 작업 폴더(현재 브랜치)의 파일과 빌드 결과입니다."
            )
        return changes, source

    if args.files:
        if args.files == "-":
            lines = sys.stdin.read().splitlines()
            desc = "직접 입력 (표준입력)"
        else:
            path = Path(args.files)
            if not path.is_file():
                raise UsageError(f"파일 목록을 찾을 수 없습니다: {path}")
            lines = path.read_text(encoding="utf-8-sig").splitlines()
            desc = f"직접 입력 ({path.name})"
    else:
        lines, desc = args.paths, "직접 입력 (명령줄)"
    changes, errors = manual.parse_lines(lines)
    if errors:
        raise UsageError("파일 목록 오류:\n  " + "\n  ".join(errors))
    return merge_changes([changes]), SourceInfo(kind="manual", description=desc)


def _check_local_has_mrs(project: ProjectConfig, source: SourceInfo) -> None:
    """로컬 저장소(빌드 대상)에 MR 머지 커밋이 들어 있는지 확인한다."""
    if not git_local.is_git_repo(project.root):
        source.warnings.append("로컬 경로가 git 저장소가 아니어서 MR 반영 여부를 확인하지 못했습니다.")
        return
    for mr in source.merge_requests:
        sha = mr.get("commit_sha")
        if not sha:
            continue
        has = git_local.contains_commit(project.root, sha)
        if has is False:
            source.warnings.append(
                f"로컬 HEAD 에 MR !{mr['iid']} 의 커밋({sha[:8]})이 없습니다. "
                "git pull 후 다시 빌드했는지 확인하세요."
            )
        elif has is None:
            source.warnings.append(
                f"로컬 저장소에 MR !{mr['iid']} 의 커밋({sha[:8]})이 없습니다. git fetch/pull 이 필요합니다."
            )


def _mark_uncommitted(project: ProjectConfig, entries: List[Entry], changes: List[FileChange],
                      source: SourceInfo) -> None:
    """커밋되지 않은 로컬 수정이 있는 파일을 경고한다 (테스트용 설정 등이 운영에 섞이는 것 방지)."""
    if not git_local.is_git_repo(project.root):
        return
    try:
        dirty = git_local.dirty_paths(project.root)
    except git_local.GitError as e:
        log.warning("git status 실패: %s", e)
        return
    if project.mode == "sync":
        # 빌드 결과는 보통 git 에 없으므로, 빌드에 들어갔을 수 있는 소스의 미커밋 수정을 본다.
        prefix = f"{project.build_dir}/"
        targets = {c.path for c in changes} if changes else dirty
        hits = sorted(d for d in dirty & targets if not d.startswith(prefix))
        if hits:
            shown = ", ".join(hits[:5]) + (f" 외 {len(hits) - 5}개" if len(hits) > 5 else "")
            source.warnings.append(f"커밋되지 않은 로컬 수정이 빌드에 들어갔을 수 있습니다: {shown}")
        return
    for e in entries:
        if e.change.path in dirty and e.artifacts:
            e.warn("커밋되지 않은 로컬 수정이 있습니다 - 저장소와 다른 내용이 배포될 수 있습니다")


def cmd_package(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    project = cfg.project(args.project)
    if not project.root.is_dir():
        raise UsageError(f"로컬 경로가 없습니다: {project.root}")

    changes, source = _collect(args, cfg, project)
    if not changes and project.mode == "changes":
        print("변경 파일이 없습니다.")
        return EXIT_OK

    source.notes.extend(mapper.collect_notices(project, changes))
    if project.mode == "sync":
        entries = sync.plan_sync(project, changes, source)
        if not any(e.artifacts for e in entries):
            _print_result(packager.build_package(project, entries, source, dry_run=True))
            print("\n빌드 결과가 없어 패키지를 만들지 않았습니다.")
            return EXIT_FAIL
    else:
        entries = mapper.plan(project, changes)
    _mark_uncommitted(project, entries, changes, source)
    blocked = any(e.status == Status.ERROR for e in entries) or (
        args.strict and (source.warnings or any(e.status == Status.WARN for e in entries))
    )
    dry_run = args.dry_run or (blocked and not args.force)
    result = packager.build_package(project, entries, source, archive=args.archive, dry_run=dry_run,
                                    source_changes=changes)

    _print_result(result)

    if not result.manifest and not result.deletes:
        print("\n서버에 반영할 파일이 없습니다.")
        return EXIT_FAIL if result.has_errors else EXIT_OK
    if args.dry_run:
        print("\n(--dry-run: 패키지를 만들지 않았습니다)")
        return EXIT_FAIL if blocked else EXIT_OK
    if dry_run:
        reason = "실패 항목" if result.has_errors else "경고(--strict)"
        print(f"\n{reason}이 있어 패키지를 만들지 않았습니다. 원인을 해결하거나, "
              "문제 항목을 제외하고 만들려면 --force 를 붙이세요.")
        return EXIT_FAIL

    print(f"\n패키지: {result.out_dir}")
    if result.archive:
        print(f"압축 파일: {result.archive}")
    print(f"보고서: {result.out_dir / 'report.md'}")
    name = result.out_dir.name
    print(f"서버 반영: '{name}' 폴더를 통째로 서버 배포 경로 밖(예: ~/deploy/{name})에 올린 뒤, 그 폴더에서")
    print(f"          ./{SCRIPT_NAME} check -> ./{SCRIPT_NAME} backup -> ./{SCRIPT_NAME} apply  (원복: rollback)")
    print(f"          (실행 권한이 없으면 chmod +x {SCRIPT_NAME}, 또는 bash {SCRIPT_NAME} check 처럼 실행)")
    if result.hooks:
        print(f"          반영 전·후 명령은 실행 전에 확인을 받습니다 (묻지 않고 실행: ./{SCRIPT_NAME} apply -y)")
    return EXIT_FAIL if result.has_errors else EXIT_OK


def _print_result(result: packager.PackageResult) -> None:
    project = result.project
    print(f"배포 방식: {project.describe_mode}")
    print(f"변경 출처: {result.source.description}")
    for w in result.source.warnings:
        print(f"[경고] {w}")
    for n in result.source.notes:
        print(f"[알림] {n}")
    for stage, commands in result.hooks.items():
        for cmd in commands:
            print(f"[명령] {stage}: {cmd}")
    print()
    entries = result.entries
    if project.mode == "sync":
        # 빌드 결과는 파일이 많으므로 문제 있는 것만 보여준다.
        entries = [e for e in entries if e.status in (Status.ERROR, Status.WARN)]
        summary = sync.summarize(result.entries)
        if summary:
            print(f"{summary} -> {project.dest or '/'} (서버와 다른 파일만 반영)")
        if project.prune:
            print(f"정리 폴더: {', '.join(project.prune)} (이번 빌드에 없는 옛 파일은 서버에서 정리)")
    for e in entries:
        print(f"{_TAG[e.status]} {e.change.change.value} {e.change.path}")
        for a in e.artifacts:
            print(f"        -> {a.target}")
        for d in e.deletes:
            print(f"        x  {d} (서버에서 삭제)")
        for m in e.messages:
            print(f"        ! {m}")
    print()
    print(
        f"성공 {result.count(Status.OK)} / 경고 {result.count(Status.WARN)} / 실패 {result.count(Status.ERROR)}"
        f" / 제외 {result.count(Status.SKIPPED)}  |  서버 반영 {len(result.manifest)}개, 서버 삭제 {len(result.deletes)}개"
    )


# ---------------------------------------------------------------------- presets
def cmd_presets(args: argparse.Namespace) -> int:
    print(f"{'프리셋':<10} {'방식':<8} 설명")
    for name, preset in PRESETS.items():
        print(f"{name:<10} {preset['mode']:<8} {preset['description']}")
    aliases = ", ".join(f"{a} -> {t}" for a, t in PRESET_ALIASES.items())
    print(f"\n별칭: {aliases}")
    print("방식: changes = 바뀐 파일만 매핑, sync = 빌드 결과 폴더 전체를 서버와 비교해 다른 파일만 반영")
    return EXIT_OK


# ---------------------------------------------------------------------- token
def cmd_token(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    if cfg.gitlab is None:
        raise UsageError("설정 파일에 gitlab 섹션이 없습니다.")
    if args.action == "status":
        src = token_source(cfg.gitlab)
        print(f"{cfg.gitlab.url}: {src or '토큰 없음'}")
        return EXIT_OK if src else EXIT_FAIL
    if args.action == "set":
        token = getpass.getpass(f"{cfg.gitlab.url} 의 GitLab 토큰 (입력 내용은 표시되지 않음): ").strip()
        if not token:
            raise UsageError("토큰이 비어 있습니다.")
        set_token(cfg.gitlab, token)
        print("OS 키링(Windows 자격 증명 관리자)에 저장했습니다.")
        return EXIT_OK
    removed = delete_token(cfg.gitlab)
    print("삭제했습니다." if removed else "저장된 토큰이 없습니다.")
    return EXIT_OK


# ---------------------------------------------------------------------- main
def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-c", "--config",
                        help=f"설정 파일 경로 (기본: ./{DEFAULT_CONFIG_NAME} 또는 {user_config_path()})")
    common.add_argument("-v", "--verbose", action="store_true", help="상세 로그 출력")

    parser = argparse.ArgumentParser(
        prog=CLI_NAME,
        description=f"{__app_name__} {__version__} - {__tagline__}\n"
                    "변경 파일을 서버 경로에 맞게 패키지로 만들고, 백업·반영·원복까지 처리합니다.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"{__app_name__} {__version__} - {__tagline__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", parents=[common], help="설정 파일 생성")
    p.add_argument("path", nargs="?", help=f"만들 파일 경로 (기본: {user_config_path()})")
    p.add_argument("--force", action="store_true", help="이미 있으면 덮어쓰기")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("import-legacy", parents=[common], help="기존 배포 툴(compiled-java-deployer) 프로젝트 설정 가져오기")
    p.add_argument("--db", help=f"기존 툴 DB 경로 (기본: {legacy.legacy_db_path()})")
    p.add_argument("--out", help=f"만들 설정 파일 (기본: {user_config_path()})")
    p.add_argument("--force", action="store_true", help="이미 있으면 덮어쓰기")
    p.set_defaults(func=cmd_import_legacy)

    p = sub.add_parser("check", parents=[common], help="설정·경로·GitLab 연결 점검")
    p.add_argument("-p", "--project", help="프로젝트 이름 (생략 시 전체)")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("mr", parents=[common], help="GitLab MR 목록")
    p.add_argument("-p", "--project", help="프로젝트 이름")
    p.add_argument("--state", default="merged", choices=["merged", "opened", "closed", "all"])
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--search", help="제목/설명 검색어")
    p.set_defaults(func=cmd_mr)

    p = sub.add_parser("package", parents=[common], help="배포 패키지 생성")
    p.add_argument("-p", "--project", help="프로젝트 이름")
    p.add_argument("--mr", type=int, nargs="+", metavar="IID", help="GitLab MR 번호 (여러 개 가능)")
    p.add_argument("--git", metavar="A..B", help="로컬 git 리비전 범위 (A 만 주면 A..HEAD)")
    p.add_argument("--files", metavar="FILE", help="파일 목록 텍스트 ('-' 이면 표준입력)")
    p.add_argument("paths", nargs="*", help="변경 파일 경로 직접 나열")
    p.add_argument("--archive", choices=["tar", "zip"], help="패키지를 압축 (tar = .tar.gz, 서버용 권장)")
    p.add_argument("--dry-run", action="store_true", help="패키지를 만들지 않고 결과만 출력")
    p.add_argument("--strict", action="store_true", help="경고도 실패로 취급")
    p.add_argument("--force", action="store_true", help="실패 항목이 있어도 나머지로 패키지 생성")
    p.set_defaults(func=cmd_package)

    p = sub.add_parser("presets", parents=[common], help="사용 가능한 프리셋 목록")
    p.set_defaults(func=cmd_presets)

    p = sub.add_parser("token", parents=[common], help="GitLab 토큰 관리 (OS 키링)")
    p.add_argument("action", choices=["set", "delete", "status"])
    p.set_defaults(func=cmd_token)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # cp949 콘솔에서도 출력이 죽지 않도록
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        return args.func(args)
    except (ConfigError, UsageError, SecretError) as e:
        print(f"[오류] {e}", file=sys.stderr)
        return EXIT_USAGE
    except (GitLabError, git_local.GitError) as e:
        print(f"[오류] {e}", file=sys.stderr)
        return EXIT_FAIL
    except KeyboardInterrupt:
        print("\n중단했습니다.", file=sys.stderr)
        return EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
