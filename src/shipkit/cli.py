"""ShipKit 명령줄 인터페이스.

  shipkit                           대화형 모드 (프로젝트 -> 변경 출처 -> 미리보기 -> 생성)
  shipkit init                      개인 설정 파일(shipkit.yaml) 생성
  shipkit init-project              프로젝트 저장소에 팀 공유 설정(.shipkit.yml) 생성
  shipkit import-legacy             기존 배포 툴(compiled-java-deployer) 프로젝트 설정 가져오기
  shipkit check   [-p 프로젝트]      설정·경로·GitLab 연결 점검
  shipkit mr      [-p 프로젝트]      GitLab MR 목록
  shipkit pr      [-p 프로젝트]      GitHub PR 목록
  shipkit package [-p 프로젝트] --mr 12 15 | --pr 7 | --git A..B | --files 목록.txt | 경로...
                                    (sync 모드 프로젝트는 변경 출처 없이도 실행 가능)
  shipkit presets                   사용 가능한 프리셋 목록
  shipkit token   set | delete | status  [gitlab | github]

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
    CLI_NAME, PROJECT_FILE, __app_name__, __tagline__, __version__, legacy, mapper, packager,
    projectfile, sync, workflow,
)
from shipkit.config import (
    _GITHUB_REPO_RE, DEFAULT_CONFIG_NAME, PRESET_ALIASES, PRESETS, Config, ConfigError, ProjectConfig,
    _parse_server_path,
    load_config, resolve_preset, user_config_path,
)
from shipkit.models import Entry, FileChange, SourceInfo, Status
from shipkit.secrets import SecretError, delete_token, set_token, token_source
from shipkit.sources import git_local
from shipkit.sources import providers
from shipkit.sources.github import GitHubError
from shipkit.sources.gitlab import GitLabError

log = logging.getLogger(CLI_NAME)

EXIT_OK, EXIT_FAIL, EXIT_USAGE = 0, 1, 2

_TAG = {Status.OK: "[성공]", Status.WARN: "[경고]", Status.ERROR: "[실패]", Status.SKIPPED: "[제외]"}


class UsageError(Exception):
    pass


# ---------------------------------------------------------------------- 공통
def _provider(cfg: Config, project: ProjectConfig, kind: str) -> providers.Provider:
    try:
        return providers.make_provider(cfg, project, kind)
    except providers.ProviderError as e:
        raise UsageError(str(e)) from e


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


def cmd_init_project(args: argparse.Namespace) -> int:
    root = projectfile.find_repo_root(Path(args.path or Path.cwd()).resolve())
    target = root / PROJECT_FILE
    if args.preset:
        resolve_preset(args.preset, "--preset")
        preset, reason = PRESET_ALIASES.get(args.preset, args.preset), "직접 지정"
    else:
        preset, reason = projectfile.detect_preset(root)
    server_path = args.server_path
    if not server_path and sys.stdin.isatty():
        server_path = input("서버 배포 경로 (예: /app/tomcat/webapps/ROOT, 비워 두면 나중에 직접 입력): ").strip()
    if server_path:
        server_path = _parse_server_path(server_path, "--server-path")
    if args.github_repo and not _GITHUB_REPO_RE.match(args.github_repo):
        raise UsageError(f"--github-repo 는 'owner/name' 형식이어야 합니다: {args.github_repo}")
    text = projectfile.render(args.name or root.name, preset, server_path or None, args.gitlab_project,
                              args.github_repo)
    _write_new_file(target, text, args.force)
    print(f"프로젝트 설정 파일을 만들었습니다: {target}")
    print(f"  프리셋: {preset} ({reason})")
    if not server_path:
        print("  [필수] server_path 를 적어야 합니다. 파일을 열어 주석(#)을 풀고 서버 경로를 넣으세요.")
    print(f"저장소에 커밋하면 팀원도 같은 설정을 씁니다. 이 폴더 안에서 '{CLI_NAME}' 을 실행하면 프로젝트를 자동으로 찾습니다.")
    print(f"  (개인 설정에 등록하지 않았다면 개인 설정의 defaults.output_dir 가 필요합니다. 예: D:/deploy/{{project}})")
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
    if args.project:
        targets = [cfg.project(args.project)]
    else:
        targets = [cfg.projects[n] for n in sorted(cfg.projects)]
        found = cfg.discover(Path.cwd().resolve())
        if found is not None and all(found.root != t.root for t in targets):
            targets.insert(0, found)
    if not targets:
        print("등록된 프로젝트가 없습니다.")
    ok = True

    def item(good: bool, msg: str) -> None:
        nonlocal ok
        ok &= good
        print(f"  {'[정상]' if good else '[문제]'} {msg}")

    for p in targets:
        print(f"\n프로젝트 {p.name}")
        item(p.root.is_dir(), f"로컬 경로: {p.root}")
        if p.project_file:
            note = " (개인 설정에 없는 프로젝트 - 이 파일로 인식)" if p.discovered else ""
            print(f"  [정보] 프로젝트 설정 파일: {p.project_file}{note}")
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
        print(f"  [정보] 서버 배포 경로: {p.server_path}"
              + (" (Windows 서버, shipkit.ps1)" if p.server_os == "windows" else ""))
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
                client = providers.gitlab_client(cfg)
                user = client.current_user()
                item(True, f"연결/인증: {user.get('username')} ({user.get('name')})")
                for p in targets:
                    if p.gitlab_project:
                        try:
                            info = client.get_project(p.gitlab_project)
                            item(True, f"{p.name} -> GitLab 프로젝트 {info.get('path_with_namespace')}")
                        except GitLabError as e:
                            item(False, f"{p.name} -> GitLab 프로젝트 {p.gitlab_project}: {e}")
            except (GitLabError, SecretError) as e:
                item(False, f"연결: {e}")

    if cfg.github:
        print(f"\nGitHub {cfg.github.url}")
        src = token_source(cfg.github)
        print(f"  [정보] 토큰: {src or '없음 (공개 저장소만 조회 가능, 조회 한도 작음)'}")
        try:
            client = providers.github_client(cfg)
            if src:
                user = client.current_user()
                item(True, f"연결/인증: {user.get('login')}")
            for p in targets:
                if p.github_repo:
                    try:
                        info = client.get_repo(p.github_repo)
                        item(True, f"{p.name} -> GitHub 저장소 {info.get('full_name')}")
                    except GitHubError as e:
                        item(False, f"{p.name} -> GitHub 저장소 {p.github_repo}: {e}")
        except (GitHubError, SecretError) as e:
            item(False, f"연결: {e}")
    print("\n점검 결과: " + ("이상 없음" if ok else "문제 있음"))
    return EXIT_OK if ok else EXIT_FAIL


# ---------------------------------------------------------------------- mr / pr
def cmd_requests(args: argparse.Namespace) -> int:
    """GitLab MR(shipkit mr) / GitHub PR(shipkit pr) 목록."""
    cfg = load_config(args.config)
    project = cfg.project(args.project)
    provider = _provider(cfg, project, args.kind)
    items = provider.list(state=args.state, limit=args.limit, search=args.search)
    if not items:
        print(f"조회된 {provider.label} 이 없습니다.")
        return EXIT_OK
    print(f"{provider.label:>6}  {'상태':<7} {'머지/수정 시각':<16}  {'작성자':<10}  제목")
    for it in items:
        when = (it.get("merged_at") or it.get("updated_at") or "")[:16].replace("T", " ")
        author = (it.get("author") or {}).get("name", "")
        ref = f"{provider.prefix}{it['iid']}"
        print(f"{ref:>6}  {it.get('state', ''):<7} {when:<16}  {author:<10}  {it.get('title', '')}")
    return EXIT_OK


# ---------------------------------------------------------------------- package
def _collect(args: argparse.Namespace, cfg: Config, project: ProjectConfig) -> Tuple[List[FileChange], SourceInfo]:
    modes = sum(bool(x) for x in (args.mr, getattr(args, "pr", None), args.git, args.files, args.paths))
    if modes == 0 and project.mode == "sync":
        return workflow.collect_build(project)
    if modes != 1:
        raise UsageError("변경 파일 출처를 하나만 지정하세요: --mr, --pr, --git, --files, 또는 경로 직접 나열")

    if args.mr or getattr(args, "pr", None):
        provider = _provider(cfg, project, "gitlab" if args.mr else "github")
        return workflow.collect_requests(provider, project, args.mr or args.pr)

    if args.git:
        return workflow.collect_git(project, args.git)

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
    try:
        return workflow.collect_lines(lines, desc)
    except workflow.InputError as e:
        raise UsageError(str(e)) from e


# 대화형 모드가 쓰는 이름 (실제 구현은 workflow)
collect_requests = workflow.collect_requests
is_blocked = workflow.is_blocked


def prepare_entries(project: ProjectConfig, changes: List[FileChange], source: SourceInfo) -> Optional[List[Entry]]:
    """배포 계획을 만든다. sync 모드에서 빌드 결과가 없으면 안내를 출력하고 None."""
    entries = workflow.plan(project, changes, source)
    if project.mode == "sync" and not workflow.has_artifacts(entries):
        _print_result(packager.build_package(project, entries, source, dry_run=True))
        print("\n빌드 결과가 없어 패키지를 만들지 않았습니다.")
        return None
    return entries


def cmd_package(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    project = cfg.project(args.project)
    if not project.root.is_dir():
        raise UsageError(f"로컬 경로가 없습니다: {project.root}")

    changes, source = _collect(args, cfg, project)
    if not changes and project.mode == "changes":
        print("변경 파일이 없습니다.")
        return EXIT_OK
    entries = prepare_entries(project, changes, source)
    if entries is None:
        return EXIT_FAIL
    return run_package(project, entries, source, changes, archive=args.archive, dry_run=args.dry_run,
                       strict=args.strict, force=args.force)


def run_package(project: ProjectConfig, entries: List[Entry], source: SourceInfo, changes: List[FileChange],
                archive: Optional[str] = None, dry_run: bool = False, strict: bool = False, force: bool = False,
                show: bool = True) -> int:
    blocked = is_blocked(entries, source, strict)
    preview_only = dry_run or (blocked and not force)
    result = packager.build_package(project, entries, source, archive=archive, dry_run=preview_only,
                                    source_changes=changes)
    if show:
        _print_result(result)

    if not result.manifest and not result.deletes:
        print("\n서버에 반영할 파일이 없습니다.")
        return EXIT_FAIL if result.has_errors else EXIT_OK
    if dry_run:
        print("\n(--dry-run: 패키지를 만들지 않았습니다)")
        return EXIT_FAIL if blocked else EXIT_OK
    if preview_only:
        reason = "실패 항목" if result.has_errors else "경고(--strict)"
        print(f"\n{reason}이 있어 패키지를 만들지 않았습니다. 원인을 해결하거나, "
              "문제 항목을 제외하고 만들려면 --force 를 붙이세요.")
        return EXIT_FAIL

    print(f"\n패키지: {result.out_dir}")
    if result.archive:
        print(f"압축 파일: {result.archive}")
    print(f"보고서: {result.out_dir / 'report.md'}")
    name = result.out_dir.name
    sh = project.script_name
    if project.server_os == "windows":
        run = f"powershell -ExecutionPolicy Bypass -File .\\{sh}"
        print(f"서버 반영: '{name}' 폴더를 서버 배포 경로 밖(예: D:\\deploy\\{name})에 복사한 뒤, 그 폴더에서")
        print(f"          {run} check -> backup -> apply  (원복: rollback)")
        if result.hooks:
            print("          반영 전·후 명령은 실행 전에 확인을 받습니다 (묻지 않고 실행: -Yes)")
    else:
        print(f"서버 반영: '{name}' 폴더를 통째로 서버 배포 경로 밖(예: ~/deploy/{name})에 올린 뒤, 그 폴더에서")
        print(f"          ./{sh} check -> ./{sh} backup -> ./{sh} apply  (원복: rollback)")
        print(f"          (실행 권한이 없으면 chmod +x {sh}, 또는 bash {sh} check 처럼 실행)")
        if result.hooks:
            print(f"          반영 전·후 명령은 실행 전에 확인을 받습니다 (묻지 않고 실행: ./{sh} apply -y)")
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


# ---------------------------------------------------------------------- start (대화형)
def cmd_start(args: argparse.Namespace) -> int:
    from shipkit.interactive import Wizard
    cfg = load_config(args.config)
    return Wizard(cfg).run(project_name=args.project)


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
    services = {"gitlab": cfg.gitlab, "github": cfg.github}
    if args.service:
        target = services[args.service]
        if target is None:
            raise UsageError(f"개인 설정에 {args.service} 섹션이 없습니다.")
        targets = [target]
    else:
        targets = [s for s in services.values() if s is not None]
        if not targets:
            raise UsageError("개인 설정에 gitlab 또는 github 섹션이 없습니다.")
        if args.action != "status" and len(targets) > 1:
            raise UsageError(f"대상을 지정하세요: '{CLI_NAME} token {args.action} gitlab' 또는 '... github'")
    if args.action == "status":
        found = True
        for svc in targets:
            src = token_source(svc)
            found &= src is not None
            print(f"{svc.label} {svc.url}: {src or '토큰 없음'}")
        return EXIT_OK if found else EXIT_FAIL
    svc = targets[0]
    if args.action == "set":
        token = getpass.getpass(f"{svc.url} 의 {svc.label} 토큰 (입력 내용은 표시되지 않음): ").strip()
        if not token:
            raise UsageError("토큰이 비어 있습니다.")
        set_token(svc, token)
        print(f"{svc.label} 토큰을 OS 키링(Windows 자격 증명 관리자)에 저장했습니다.")
        return EXIT_OK
    removed = delete_token(svc)
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
    parser.add_argument("-c", "--config", dest="top_config", metavar="CONFIG", help="설정 파일 경로 (대화형 모드용)")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("init", parents=[common], help="개인 설정 파일 생성")
    p.add_argument("path", nargs="?", help=f"만들 파일 경로 (기본: {user_config_path()})")
    p.add_argument("--force", action="store_true", help="이미 있으면 덮어쓰기")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("init-project", parents=[common], help=f"프로젝트 저장소에 팀 공유 설정({PROJECT_FILE}) 생성")
    p.add_argument("path", nargs="?", help="프로젝트 폴더 (기본: 현재 폴더가 속한 git 저장소)")
    p.add_argument("--preset", help="프리셋 (생략하면 저장소를 보고 추정)")
    p.add_argument("--server-path", help="서버 배포 경로")
    p.add_argument("--gitlab-project", help="GitLab 프로젝트 ID 또는 그룹/프로젝트")
    p.add_argument("--github-repo", help="GitHub 저장소 (owner/name)")
    p.add_argument("--name", help="프로젝트 이름 (기본: 폴더 이름)")
    p.add_argument("--force", action="store_true", help="이미 있으면 덮어쓰기")
    p.set_defaults(func=cmd_init_project)

    p = sub.add_parser("start", parents=[common], help="대화형 모드 (인자 없이 실행한 것과 같음)")
    p.add_argument("-p", "--project", help="프로젝트 이름")
    p.set_defaults(func=cmd_start)

    p = sub.add_parser("import-legacy", parents=[common], help="기존 배포 툴(compiled-java-deployer) 프로젝트 설정 가져오기")
    p.add_argument("--db", help=f"기존 툴 DB 경로 (기본: {legacy.legacy_db_path()})")
    p.add_argument("--out", help=f"만들 설정 파일 (기본: {user_config_path()})")
    p.add_argument("--force", action="store_true", help="이미 있으면 덮어쓰기")
    p.set_defaults(func=cmd_import_legacy)

    p = sub.add_parser("check", parents=[common], help="설정·경로·GitLab/GitHub 연결 점검")
    p.add_argument("-p", "--project", help="프로젝트 이름 (생략 시 전체)")
    p.set_defaults(func=cmd_check)

    for name, kind, label in (("mr", "gitlab", "GitLab MR"), ("pr", "github", "GitHub PR")):
        p = sub.add_parser(name, parents=[common], help=f"{label} 목록")
        p.add_argument("-p", "--project", help="프로젝트 이름")
        p.add_argument("--state", default="merged", choices=["merged", "opened", "closed", "all"])
        p.add_argument("--limit", type=int, default=20)
        p.add_argument("--search", help="제목 검색어")
        p.set_defaults(func=cmd_requests, kind=kind)

    p = sub.add_parser("package", parents=[common], help="배포 패키지 생성")
    p.add_argument("-p", "--project", help="프로젝트 이름")
    p.add_argument("--mr", type=int, nargs="+", metavar="IID", help="GitLab MR 번호 (여러 개 가능)")
    p.add_argument("--pr", type=int, nargs="+", metavar="NUM", help="GitHub PR 번호 (여러 개 가능)")
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

    p = sub.add_parser("token", parents=[common], help="GitLab/GitHub 토큰 관리 (OS 키링)")
    p.add_argument("action", choices=["set", "delete", "status"])
    p.add_argument("service", nargs="?", choices=["gitlab", "github"], help="대상 (설정이 하나뿐이면 생략)")
    p.set_defaults(func=cmd_token)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # cp949 콘솔에서도 출력이 죽지 않도록
        except (AttributeError, ValueError):
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        if not sys.stdin.isatty():
            parser.print_help()
            return EXIT_USAGE
        args = parser.parse_args(["start"] + (["-c", args.top_config] if args.top_config else []))
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        return args.func(args)
    except (ConfigError, UsageError, SecretError) as e:
        print(f"[오류] {e}", file=sys.stderr)
        return EXIT_USAGE
    except (GitLabError, GitHubError, git_local.GitError) as e:
        print(f"[오류] {e}", file=sys.stderr)
        return EXIT_FAIL
    except KeyboardInterrupt:
        print("\n중단했습니다.", file=sys.stderr)
        return EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
