"""배포 보고서(report.md) 생성."""
from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

from shipkit import __app_name__, __tagline__, __version__
from shipkit.models import ChangeType, Status

if TYPE_CHECKING:
    from shipkit.packager import PackageResult

_ICON = {Status.OK: "✅", Status.WARN: "⚠️", Status.ERROR: "❌", Status.SKIPPED: "➖"}
# 트리가 이보다 길면(빌드 결과 동기화 등) 최상위 폴더별 개수로 줄여서 보여준다.
TREE_LIMIT = 200


def _cell(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def render(result: "PackageResult") -> str:
    p = result.project
    entries = result.entries
    sync = p.mode == "sync"
    lines: List[str] = []
    w = lines.append

    w(f"# 배포 패키지 보고서 - {p.name}")
    w("")
    w(f"> {__app_name__} {__version__} - {__tagline__}")
    w("")
    w(f"- **생성 시각**: {result.created_at:%Y-%m-%d %H:%M:%S}")
    if result.out_dir:
        w(f"- **패키지 경로**: `{result.out_dir}`")
    w(f"- **서버 배포 경로**: `{p.server_path}`")
    w(f"- **배포 방식**: {p.describe_mode}")
    w(f"- **변경 출처**: {result.source.description}")
    w("")

    w("## 요약")
    w("")
    w("| 항목 | 개수 |")
    w("|:---|---:|")
    if sync:
        changes = result.source_changes
        by_change = Counter(c.change for c in changes)
        if changes:
            w(f"| 소스 변경 (추가/수정/삭제) | {len(changes)} "
              f"({by_change[ChangeType.ADDED]}/{by_change[ChangeType.MODIFIED]}/{by_change[ChangeType.DELETED]}) |")
        w(f"| 빌드 결과 파일 (패키지에 포함) | {len(result.manifest)} |")
        w(f"| 정리 폴더 | {', '.join(p.prune) or '없음'} |")
    else:
        by_change = Counter(e.change.change for e in entries)
        w(f"| 변경 파일 (추가/수정/삭제) | {len(entries)} "
          f"({by_change[ChangeType.ADDED]}/{by_change[ChangeType.MODIFIED]}/{by_change[ChangeType.DELETED]}) |")
        w(f"| 서버 반영 파일 | {len(result.manifest)} |")
        w(f"| 서버 삭제 파일 | {len(result.deletes)} |")
    for st in (Status.OK, Status.WARN, Status.ERROR, Status.SKIPPED):
        w(f"| {_ICON[st]} {st.value} | {result.count(st)} |")
    w("")

    problems = [e for e in entries if e.status in (Status.ERROR, Status.WARN)]
    if result.source.warnings or problems:
        w("## 확인 필요")
        w("")
        for msg in result.source.warnings:
            w(f"- ⚠️ {msg}")
        for e in sorted(problems, key=lambda e: (e.status != Status.ERROR, e.change.path)):
            w(f"- {_ICON[e.status]} `{e.change.path}`: {'; '.join(e.messages)}")
        w("")

    if result.source.notes:
        w("## 알림 (패키지 외에 따로 할 일)")
        w("")
        for msg in result.source.notes:
            w(f"- 📌 {msg}")
        w("")

    if result.hooks:
        w("## 서버에서 실행할 명령")
        w("")
        w("apply·rollback 때 실행 전에 확인을 받습니다(`-y` 로 생략, `--no-hooks` 로 실행 안 함). "
          "서버 배포 경로에서 실행되고, 명령 하나라도 실패하면 거기서 멈춥니다.")
        w("")
        labels = {"pre_apply": "반영 전 (실패하면 apply 중단)", "post_apply": "반영 후",
                  "post_rollback": "원복 후"}
        for stage, commands in result.hooks.items():
            w(f"**{stage}** - {labels.get(stage, stage)}")
            w("")
            w("```bash")
            for cmd in commands:
                w(cmd)
            w("```")
            w("")

    if result.source.merge_requests:
        label = result.source.request_label
        short = "PR" if label == "Pull Request" else "MR"
        w(f"## {label}")
        w("")
        w(f"| {short} | 제목 | 작성자 | 상태 | 머지 시각 | 브랜치 | 파일 |")
        w("|:---|:---|:---|:---:|:---|:---|---:|")
        for mr in result.source.merge_requests:
            ref = mr.get("ref") or f"!{mr['iid']}"
            link = f"[{ref}]({mr['web_url']})" if mr.get("web_url") else ref
            merged = (mr.get("merged_at") or "")[:16].replace("T", " ")
            branch = f"{mr.get('source_branch', '')} → {mr.get('target_branch', '')}"
            w(f"| {link} | {_cell(mr['title'])} | {_cell(mr['author'])} | {mr['state']} | {merged} "
              f"| {_cell(branch)} | {mr['file_count']} |")
        w("")

    if sync:
        if result.source_changes:
            w("## 소스 변경 내역")
            w("")
            w("| 구분 | 소스 경로 | 출처 |")
            w("|:---:|:---|:---|")
            for c in result.source_changes:
                w(f"| {c.change.label} | `{_cell(c.path)}` | {_cell(c.origin)} |")
            w("")
    else:
        w("## 변경 파일별 처리 결과")
        w("")
        w("| 상태 | 구분 | 소스 경로 | 서버 반영/삭제 파일 | 출처 | 비고 |")
        w("|:---:|:---:|:---|:---|:---|:---|")
        for e in entries:
            outputs = [a.target for a in e.artifacts] + [f"(삭제) {d}" for d in e.deletes]
            w(f"| {_ICON[e.status]} | {e.change.change.label} | `{_cell(e.change.path)}` "
              f"| {'<br>'.join(_cell(o) for o in outputs) or '-'} | {_cell(e.change.origin)} "
              f"| {_cell('; '.join(e.messages)) or ''} |")
        w("")

    w("## 패키지에 담긴 파일 구조" if sync else "## 서버 반영 파일 구조")
    w("")
    w("```")
    w(_tree(result.manifest) if result.manifest else "(없음)")
    w("```")
    w("")
    if result.deletes:
        w("## 서버 삭제 파일")
        w("")
        for d in result.deletes:
            w(f"- `{d}`")
        w("")

    w("## 서버 반영 절차")
    w("")
    _write_deploy_steps(w, result)
    return "\n".join(lines)


def server_steps(result: "PackageResult") -> Tuple[str, List[Tuple[str, str]], Tuple[str, str]]:
    """화면용 서버 반영 절차: (올리기 안내, [(명령, 설명)...], (원복 명령, 설명)). 아래 보고서 절차와 같은 내용."""
    p = result.project
    name = result.out_dir.name if result.out_dir else "<패키지 폴더>"
    return server_steps_for(name, p.server_path, p.server_os, p.script_name, result.archive_kind)


def server_steps_for(name: str, server_path: str, server_os: str, script_name: str,
                     archive_kind: Optional[str]) -> Tuple[str, List[Tuple[str, str]], Tuple[str, str]]:
    if server_os == "windows":
        run = f"powershell -ExecutionPolicy Bypass -File .\\{script_name}"
        upload = f"{name} 폴더를 서버의 배포 경로 밖(예: D:\\deploy)에 통째로 복사합니다."
        steps = [(f"cd D:\\deploy\\{name}", "패키지 폴더로 이동")]
        if archive_kind == "zip":
            steps.insert(0, (f"Expand-Archive {name}.zip", "압축 풀기"))
        steps += [(f"{run} check", "반영 내용 확인 (서버 변경 없음)"), (f"{run} backup", "기존 파일 보관"),
                  (f"{run} apply", f"{server_path} 에 반영")]
        return upload, steps, (f"{run} rollback", "문제가 생기면 apply 이전으로 되돌리기")
    sh = script_name
    upload = f"{name} 폴더를 서버의 배포 경로 밖(예: ~/deploy)에 통째로 올립니다."
    steps = [(f"cd ~/deploy/{name}", "패키지 폴더로 이동")]
    if archive_kind == "tar":
        steps.insert(0, (f"tar -xzf {name}.tar.gz", "압축 풀기"))
    else:
        steps.append((f"chmod +x {sh}", "실행 권한 (처음 한 번)"))
    steps += [(f"./{sh} check", "반영 내용 확인 (서버 변경 없음)"), (f"./{sh} backup", "기존 파일 보관"),
              (f"./{sh} apply", f"{server_path} 에 반영")]
    return upload, steps, (f"./{sh} rollback", "문제가 생기면 apply 이전으로 되돌리기")


def _write_deploy_steps(w, result: "PackageResult") -> None:
    if result.project.server_os == "windows":
        _write_deploy_steps_windows(w, result)
        return
    p = result.project
    sh = p.script_name
    name = result.out_dir.name if result.out_dir else "<패키지 폴더>"
    top_dirs = sorted({path.split("/")[0] for path in result.manifest})
    contents = ", ".join(f"`{d}`" for d in top_dirs[:10]) or "(없음)"

    w(f"**패키지 폴더 = `{name}`** (이 보고서가 들어 있는 폴더)")
    w("")
    w("```")
    w(f"{name}/")
    w(f"├─ files/          배포할 파일 ({', '.join(top_dirs[:10]) or '없음'}) - {sh} 가 서버 배포 경로로 복사")
    w(f"├─ {sh:<14}서버에서 실행할 스크립트")
    w("├─ manifest.txt    반영할 파일 목록")
    w("├─ delete.txt      서버에서 삭제할 파일 목록")
    if p.mode == "sync":
        w("├─ prune.txt       옛 빌드 파일을 정리할 서버 폴더 목록")
    if result.hooks:
        w("├─ hooks/          반영 전·후에 서버에서 실행할 명령")
    w("├─ checksums.sha256  패키지 파일 검증용")
    w("└─ report.md       이 보고서")
    w("```")
    w("")
    w(f"1. `{name}` 폴더를 **통째로** 서버에 올립니다.")
    w(f"   - 서버 배포 경로(`{p.server_path}`) **밖**에 올리세요. 예: `~/deploy/{name}`")
    w(f"   - 안에 올리면 {sh} 와 보고서가 웹 경로에 섞입니다.")
    if result.archive_kind == "tar":
        w(f"   - 압축 파일로 올렸다면 먼저 풀기: `tar -xzf {name}.tar.gz`")
    elif result.archive_kind == "zip":
        w(f"   - 압축 파일로 올렸다면 먼저 풀기: `unzip {name}.zip`")
    w(f"2. 서버에서 `{name}` 폴더로 이동해 순서대로 실행합니다.")
    w("")
    w("```bash")
    w(f"cd ~/deploy/{name}")
    if result.archive_kind != "tar":
        w(f"chmod +x {sh:<16}# 실행 권한 부여 (처음 한 번)")
    w(f"./{sh} check         # 1. 반영 내용 확인 (서버 변경 없음)")
    w(f"./{sh} backup        # 2. 바뀌거나 지워질 기존 파일을 {name}/backup/ 에 보관")
    w(f"./{sh} apply         # 3. files/ 내용을 {p.server_path} 에 반영")
    w(f"./{sh} rollback      # 문제 시 apply 이전으로 원복")
    w(f"./{sh} history       # 이 서버의 배포 이력")
    w("```")
    w("")
    w("check·backup·apply 전에 `checksums.sha256` 으로 패키지 파일이 업로드 중 손상되지 않았는지 검증합니다. "
      "같은 대상에 다른 배포가 진행 중이면 backup·apply·rollback 이 멈춥니다.")
    w("")
    s = p.server
    if s.owner or s.file_mode or s.dir_mode:
        w(f"새로 만들어지는 파일·폴더는 소유자 `{s.owner or '그대로'}`, 파일 권한 `{s.file_mode or '그대로'}`, "
          f"폴더 권한 `{s.dir_mode or '그대로'}` 로 맞춥니다. 기존 파일은 원래 소유자·권한이 유지됩니다.")
        w("")
    if result.archive_kind == "tar":
        w("tar.gz 로 올리면 실행 권한이 이미 들어 있어 `chmod` 가 필요 없습니다. ")
    w(f"`chmod` 없이 `bash {sh} check` 처럼 실행해도 됩니다.")
    w("")
    w(f"`{sh}` 는 `files/` 아래 경로를 그대로 `{p.server_path}/` 아래에 복사하고, 서버와 내용이 같은 파일은 건드리지 않습니다. "
      f"다른 경로에 반영하려면 두 번째 인자로 지정합니다: `./{sh} apply /다른/경로`")
    w("")
    if p.mode == "sync" and p.prune:
        folders = ", ".join(f"`{d}`" for d in p.prune)
        w(f"**옛 빌드 파일 정리**: 서버의 {folders} 폴더 안에서 이번 빌드에 없는 파일은 apply 때 삭제됩니다. "
          f"`check` 에서 `[정리예정]` 으로 미리 확인할 수 있고, 삭제된 파일도 백업되어 `rollback` 으로 되살아납니다.")
        w("")
    w(f"> 스크립트 없이 직접 옮기려면 `files/` **안의 내용**({contents})을 `{p.server_path}` 로 복사하세요. "
      "이 경우 백업, 원복, 옛 파일 정리는 직접 해야 합니다.")
    w("")


def _write_deploy_steps_windows(w, result: "PackageResult") -> None:
    p = result.project
    ps = p.script_name
    name = result.out_dir.name if result.out_dir else "<패키지 폴더>"
    run = f"powershell -ExecutionPolicy Bypass -File .\\{ps}"
    top_dirs = sorted({path.split("/")[0] for path in result.manifest})
    contents = ", ".join(f"`{d}`" for d in top_dirs[:10]) or "(없음)"

    w(f"**패키지 폴더 = `{name}`** (이 보고서가 들어 있는 폴더) - Windows 서버용")
    w("")
    w("```")
    w(f"{name}\\")
    w(f"├─ files\\          배포할 파일 ({', '.join(top_dirs[:10]) or '없음'}) - {ps} 가 서버 배포 경로로 복사")
    w(f"├─ {ps:<15}서버에서 실행할 PowerShell 스크립트")
    w("├─ manifest.txt    반영할 파일 목록")
    w("├─ delete.txt      서버에서 삭제할 파일 목록")
    if p.mode == "sync":
        w("├─ prune.txt       옛 빌드 파일을 정리할 서버 폴더 목록")
    if result.hooks:
        w("├─ hooks\\          반영 전·후에 서버에서 실행할 PowerShell 명령")
    w("├─ checksums.sha256  패키지 파일 검증용")
    w("└─ report.md       이 보고서")
    w("```")
    w("")
    w(f"1. `{name}` 폴더를 **통째로** 서버에 복사합니다 (원격 데스크톱, 공유 폴더 등).")
    w(f"   - 서버 배포 경로(`{p.server_path}`) **밖**에 두세요. 예: `D:\\deploy\\{name}`")
    if result.archive_kind == "zip":
        w(f"   - 압축 파일로 옮겼다면 먼저 풀기: 탐색기에서 압축 풀기 또는 `Expand-Archive {name}.zip`")
    w("   - 인터넷·메일로 받은 파일이면 Windows 가 실행을 막을 수 있습니다: "
      "PowerShell 에서 `Get-ChildItem -Recurse | Unblock-File`")
    w(f"2. 서버에서 PowerShell 을 열고 `{name}` 폴더로 이동해 순서대로 실행합니다.")
    w("")
    w("```powershell")
    w(f"cd D:\\deploy\\{name}")
    w(f"{run} check      # 1. 반영 내용 확인 (서버 변경 없음)")
    w(f"{run} backup     # 2. 바뀌거나 지워질 기존 파일을 backup\\ 에 보관")
    w(f"{run} apply      # 3. files\\ 내용을 {p.server_path} 에 반영")
    w(f"{run} rollback   # 문제 시 apply 이전으로 원복")
    w(f"{run} history    # 이 서버의 배포 이력")
    w("```")
    w("")
    w("`-ExecutionPolicy Bypass` 는 이번 실행에만 서명되지 않은 스크립트를 허용합니다(서버 설정은 바뀌지 않음). "
      "반영 전·후 명령을 묻지 않고 실행하려면 `-Yes`, 실행하지 않으려면 `-NoHooks` 를 붙입니다.")
    w("")
    w("check·backup·apply 전에 `checksums.sha256` 으로 패키지 파일이 손상되지 않았는지 검증하고, "
      "같은 대상에 다른 배포가 진행 중이면 멈춥니다. 서버와 내용이 같은 파일은 건드리지 않습니다.")
    w("")
    if p.mode == "sync" and p.prune:
        folders = ", ".join(f"`{d}`" for d in p.prune)
        w(f"**옛 빌드 파일 정리**: 서버의 {folders} 폴더 안에서 이번 빌드에 없는 파일은 apply 때 삭제됩니다. "
          f"`check` 에서 `[정리예정]` 으로 미리 확인할 수 있고, 삭제된 파일도 백업되어 `rollback` 으로 되살아납니다.")
        w("")
    w(f"> 스크립트 없이 직접 옮기려면 `files\\` **안의 내용**({contents})을 `{p.server_path}` 로 복사하세요. "
      "이 경우 백업, 원복, 옛 파일 정리는 직접 해야 합니다.")
    w("")


def _tree(paths: List[str]) -> str:
    if len(paths) > TREE_LIMIT:
        counts = Counter(p.split("/")[0] if "/" in p else "." for p in paths)
        lines = [f"(파일 {len(paths)}개 - 최상위 폴더별 개수)"]
        lines += [f"{name + '/' if name != '.' else '(루트)'}  {n}개" for name, n in sorted(counts.items())]
        return "\n".join(lines)
    root: Dict[str, dict] = {}
    for path in paths:
        node = root
        for part in path.split("/"):
            node = node.setdefault(part, {})
    out = ["."]

    def walk(node: Dict[str, dict], prefix: str) -> None:
        # 폴더 먼저, 그 다음 파일
        items = sorted(node.items(), key=lambda kv: (not kv[1], kv[0]))
        for i, (name, child) in enumerate(items):
            last = i == len(items) - 1
            out.append(f"{prefix}{'└── ' if last else '├── '}{name}")
            if child:
                walk(child, prefix + ("    " if last else "│   "))

    walk(root, "")
    return "\n".join(out)


def write_report(path: Path, result: "PackageResult") -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(render(result))
