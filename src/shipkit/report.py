"""배포 보고서(report.md) 생성."""
from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List

from shipkit import __app_name__, __tagline__, __version__
from shipkit.models import ChangeType, Status

if TYPE_CHECKING:
    from shipkit.packager import PackageResult

_ICON = {Status.OK: "✅", Status.WARN: "⚠️", Status.ERROR: "❌", Status.SKIPPED: "➖"}


def _cell(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def render(result: "PackageResult") -> str:
    p = result.project
    entries = result.entries
    by_change = Counter(e.change.change for e in entries)
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
    w(f"- **변경 출처**: {result.source.description}")
    w("")

    w("## 요약")
    w("")
    w("| 항목 | 개수 |")
    w("|:---|---:|")
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

    if result.source.merge_requests:
        w("## Merge Request")
        w("")
        w("| MR | 제목 | 작성자 | 상태 | 머지 시각 | 브랜치 | 파일 |")
        w("|:---|:---|:---|:---:|:---|:---|---:|")
        for mr in result.source.merge_requests:
            link = f"[!{mr['iid']}]({mr['web_url']})" if mr.get("web_url") else f"!{mr['iid']}"
            merged = (mr.get("merged_at") or "")[:16].replace("T", " ")
            branch = f"{mr.get('source_branch', '')} → {mr.get('target_branch', '')}"
            w(f"| {link} | {_cell(mr['title'])} | {_cell(mr['author'])} | {mr['state']} | {merged} "
              f"| {_cell(branch)} | {mr['file_count']} |")
        w("")

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

    w("## 서버 반영 파일 구조")
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


def _write_deploy_steps(w, result: "PackageResult") -> None:
    p = result.project
    name = result.out_dir.name if result.out_dir else "<패키지 폴더>"
    top_dirs = sorted({path.split("/")[0] for path in result.manifest})
    contents = ", ".join(f"`{d}`" for d in top_dirs) or "(없음)"

    w(f"**패키지 폴더 = `{name}`** (이 보고서가 들어 있는 폴더)")
    w("")
    w("```")
    w(f"{name}/")
    w(f"├─ files/          배포할 파일 ({', '.join(top_dirs) or '없음'}) - shipkit.sh 가 서버 배포 경로로 복사")
    w("├─ shipkit.sh      서버에서 실행할 스크립트")
    w("├─ manifest.txt    반영할 파일 목록")
    w("├─ delete.txt      서버에서 삭제할 파일 목록")
    w("└─ report.md       이 보고서")
    w("```")
    w("")
    w(f"1. `{name}` 폴더를 **통째로** 서버에 올립니다.")
    w(f"   - 서버 배포 경로(`{p.server_path}`) **밖**에 올리세요. 예: `~/deploy/{name}`")
    w(f"   - 안에 올리면 shipkit.sh 와 보고서가 웹 경로에 섞입니다.")
    if result.archive_kind == "tar":
        w(f"   - 압축 파일로 올렸다면 먼저 풀기: `tar -xzf {name}.tar.gz`")
    elif result.archive_kind == "zip":
        w(f"   - 압축 파일로 올렸다면 먼저 풀기: `unzip {name}.zip`")
    w(f"2. 서버에서 `{name}` 폴더로 이동해 순서대로 실행합니다.")
    w("")
    w("```bash")
    w(f"cd ~/deploy/{name}")
    if result.archive_kind != "tar":
        w("chmod +x shipkit.sh        # 실행 권한 부여 (처음 한 번)")
    w("./shipkit.sh check         # 1. 반영 내용 확인 (서버 변경 없음)")
    w(f"./shipkit.sh backup        # 2. 바뀔 기존 파일을 {name}/backup/ 에 보관")
    w(f"./shipkit.sh apply         # 3. files/ 내용을 {p.server_path} 에 반영")
    w("./shipkit.sh rollback      # 문제 시 apply 이전으로 원복")
    w("```")
    w("")
    if result.archive_kind == "tar":
        w("tar.gz 로 올리면 실행 권한이 이미 들어 있어 `chmod` 가 필요 없습니다. ")
    w("`chmod` 없이 `bash shipkit.sh check` 처럼 실행해도 됩니다.")
    w("")
    w(f"`shipkit.sh` 는 `files/` 아래 경로를 그대로 `{p.server_path}/` 아래에 복사합니다. "
      f"다른 경로에 반영하려면 두 번째 인자로 지정합니다: `./shipkit.sh apply /다른/경로`")
    w("")
    w(f"> 스크립트 없이 직접 옮기려면 `files/` **안의 내용**({contents})을 `{p.server_path}` 로 복사하세요. "
      "이 경우 백업과 원복은 직접 해야 합니다.")
    w("")


def _tree(paths: List[str]) -> str:
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
