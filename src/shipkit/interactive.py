"""대화형 모드: 인자 없이 실행하면 프로젝트 -> 변경 출처 -> 미리보기 -> 생성 순서로 묻는다.

명령어에 익숙하지 않은 사람도 쓸 수 있게 하는 것이 목적이다. 실제 처리는 cli 의 함수를 그대로 쓴다.
"""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

from shipkit import __app_name__, __tagline__, __version__, cli, packager
from shipkit.config import Config, ConfigError, ProjectConfig
from shipkit.models import FileChange, SourceInfo, merge_changes
from shipkit.sources import git_local, manual
from shipkit.sources.gitlab import collect_from_mrs

YES = ("y", "yes", "ㅛ")
NO = ("n", "no", "ㅜ")
QUIT = ("q", "quit", "exit", "종료")
MR_LIST_SIZE = 15


class Cancelled(Exception):
    pass


class Wizard:
    def __init__(self, cfg: Config, ask: Callable[[str], str] = input, out: Callable[..., None] = print,
                 cwd: Optional[Path] = None):
        self.cfg = cfg
        self.ask = ask
        self.out = out
        self.cwd = (cwd or Path.cwd()).resolve()

    # ------------------------------------------------------------------ 입력 도우미
    def _input(self, prompt: str) -> str:
        answer = self.ask(prompt).strip()
        if answer.lower() in QUIT:
            raise Cancelled()
        return answer

    def choose(self, prompt: str, options: Sequence[str], default: Optional[int] = None) -> int:
        """번호로 고르게 한다. 반환값은 0부터 시작하는 인덱스."""
        for i, option in enumerate(options, 1):
            self.out(f"  {i}) {option}")
        hint = f" [기본 {default + 1}]" if default is not None else ""
        while True:
            answer = self._input(f"{prompt}{hint}: ")
            if not answer and default is not None:
                return default
            if answer.isdigit() and 1 <= int(answer) <= len(options):
                return int(answer) - 1
            self.out(f"  1~{len(options)} 사이 번호를 입력하세요. (그만두려면 q)")

    def confirm(self, prompt: str, default: bool) -> bool:
        while True:
            answer = self._input(f"{prompt} [{'Y/n' if default else 'y/N'}]: ").lower()
            if not answer:
                return default
            if answer in YES:
                return True
            if answer in NO:
                return False
            self.out("  y 또는 n 으로 답하세요.")

    # ------------------------------------------------------------------ 흐름
    def run(self, project_name: Optional[str] = None) -> int:
        self.out(f"{__app_name__} {__version__} - {__tagline__}")
        self.out("언제든 q 를 입력하면 그만둡니다.\n")
        try:
            project = self.pick_project(project_name)
            if not project.root.is_dir():
                raise cli.UsageError(f"로컬 경로가 없습니다: {project.root}")
            picked = self.pick_source(project)
            changes, source = picked
            if not changes and project.mode == "changes":
                self.out("변경 파일이 없습니다.")
                return cli.EXIT_OK
            self.out("")
            entries = cli.prepare_entries(project, changes, source)
            if entries is None:
                return cli.EXIT_FAIL
            preview = packager.build_package(project, entries, source, dry_run=True, source_changes=changes)
            cli._print_result(preview)
            if not preview.manifest and not preview.deletes:
                self.out("\n서버에 반영할 파일이 없습니다.")
                return cli.EXIT_FAIL if preview.has_errors else cli.EXIT_OK
            self.out("")
            force = False
            if cli.is_blocked(entries, source, strict=False):
                if not self.confirm("실패 항목이 있습니다. 실패 항목을 빼고 나머지로 패키지를 만들까요?", default=False):
                    self.out("패키지를 만들지 않았습니다. 원인을 해결한 뒤 다시 실행하세요.")
                    return cli.EXIT_FAIL
                force = True
            elif not self.confirm("이대로 패키지를 만들까요?", default=True):
                self.out("패키지를 만들지 않았습니다.")
                return cli.EXIT_OK
            archive = "tar" if self.confirm("서버에 올리기 쉽게 .tar.gz 압축 파일도 만들까요?", default=False) else None
            return cli.run_package(project, entries, source, changes, archive=archive, force=force, show=False)
        except Cancelled:
            self.out("\n그만뒀습니다.")
            return cli.EXIT_OK

    def pick_project(self, project_name: Optional[str]) -> ProjectConfig:
        if project_name:
            return self.cfg.project(project_name)
        candidates: List[ProjectConfig] = [self.cfg.projects[n] for n in sorted(self.cfg.projects)]
        detected: Optional[ProjectConfig] = None
        try:
            detected = self.cfg.project(None, self.cwd)
        except ConfigError:
            pass
        if detected is not None and all(detected.root != c.root for c in candidates):
            candidates.insert(0, detected)
        if not candidates:
            raise ConfigError("프로젝트가 없습니다. 개인 설정에 프로젝트를 등록하거나 프로젝트 폴더 안에서 실행하세요.")
        if len(candidates) == 1:
            self.out(f"프로젝트: {candidates[0].name} ({candidates[0].describe_mode})")
            return candidates[0]
        default = next((i for i, c in enumerate(candidates) if detected and c.root == detected.root), None)
        self.out("[1/3] 프로젝트")
        idx = self.choose("프로젝트 번호", [f"{c.name}  -  {c.root}  ({c.describe_mode})" for c in candidates], default)
        return candidates[idx]

    def pick_source(self, project: ProjectConfig) -> Tuple[List[FileChange], SourceInfo]:
        options: List[Tuple[str, str]] = []
        if project.mode == "sync":
            options.append(("build", f"빌드 결과 전체 ({project.build_dir}) - 변경 목록 없이"))
        if self.cfg.gitlab and project.gitlab_project:
            options.append(("mr", "GitLab MR 고르기"))
        if git_local.is_git_repo(project.root):
            options.append(("git", "git 범위 (태그·커밋 사이에 바뀐 파일)"))
        options.append(("files", "파일 목록 붙여넣기"))
        self.out("\n[2/3] 무엇을 배포할까요?")
        key = options[self.choose("번호", [label for _, label in options], default=0)][0]
        empty = argparse.Namespace(mr=None, git=None, files=None, paths=[])
        if key == "build":
            return cli._collect(empty, self.cfg, project)
        if key == "mr":
            return self.pick_mrs(project)
        if key == "git":
            return self.pick_git_range(project)
        return self.paste_files()

    def pick_mrs(self, project: ProjectConfig) -> Tuple[List[FileChange], SourceInfo]:
        client = cli._client(self.cfg)
        gp = cli._gitlab_project(project)
        state = "merged"
        while True:
            mrs = client.list_merge_requests(gp, state=state, limit=MR_LIST_SIZE)
            label = "머지된" if state == "merged" else "열린"
            self.out(f"\n최근 {label} MR")
            if not mrs:
                self.out("  (없음)")
            for i, mr in enumerate(mrs, 1):
                when = (mr.get("merged_at") or mr.get("updated_at") or "")[:16].replace("T", " ")
                author = (mr.get("author") or {}).get("name", "")
                self.out(f"  {i:>2}) !{mr['iid']:<6} {when}  {author:<8}  {mr.get('title', '')}")
            other = "o = 열린 MR 보기" if state == "merged" else "m = 머지된 MR 보기"
            answer = self._input(f"MR 을 고르세요 (목록 번호, 여러 개는 띄어서 / 목록에 없으면 !MR번호 / {other}): ")
            if answer.lower() in ("o", "m"):
                state = "opened" if answer.lower() == "o" else "merged"
                continue
            iids = parse_mr_selection(answer, [mr["iid"] for mr in mrs])
            if iids:
                break
            self.out("  선택한 MR 이 없습니다. 예: 1 3  또는  !612")
        changes, source = collect_from_mrs(client, gp, iids)
        cli._check_local_has_mrs(project, source)
        return changes, source

    def pick_git_range(self, project: ProjectConfig) -> Tuple[List[FileChange], SourceInfo]:
        default = None
        try:
            tag = git_local.run_git(project.root, "describe", "--tags", "--abbrev=0").strip()
            default = f"{tag}..HEAD" if tag else None
        except git_local.GitError:
            pass
        hint = f" [기본 {default}]" if default else ""
        while True:
            rng = self._input(f"범위를 입력하세요 (예: v1.2..HEAD, abc123..def456){hint}: ") or default
            if not rng:
                continue
            ns = argparse.Namespace(mr=None, git=rng, files=None, paths=[])
            try:
                return cli._collect(ns, self.cfg, project)
            except git_local.GitError as e:
                self.out(f"  {e}")

    def paste_files(self) -> Tuple[List[FileChange], SourceInfo]:
        while True:
            self.out("파일 경로를 한 줄에 하나씩 붙여넣고, 빈 줄에서 Enter 를 누르세요. (git diff --name-status 출력도 됩니다)")
            lines = []
            while True:
                line = self.ask("> ")
                if not line.strip():
                    break
                lines.append(line)
            changes, errors = manual.parse_lines(lines)
            if errors:
                self.out("  잘못된 줄이 있습니다:\n  " + "\n  ".join(errors))
                continue
            if changes:
                return merge_changes([changes]), SourceInfo(kind="manual", description="직접 입력 (붙여넣기)")
            self.out("  입력한 파일이 없습니다.")


def parse_mr_selection(answer: str, listed: List[int]) -> List[int]:
    """'1 3' -> 목록의 1·3번째 MR, '!612' -> MR 612. 잘못된 항목은 무시한다."""
    iids: List[int] = []
    for token in answer.replace(",", " ").split():
        if token.startswith("!") and token[1:].isdigit():
            iids.append(int(token[1:]))
        elif token.isdigit() and 1 <= int(token) <= len(listed):
            iids.append(listed[int(token) - 1])
    return list(dict.fromkeys(iids))
