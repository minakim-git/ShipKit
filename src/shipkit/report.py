"""배포 보고서: 패키지 결과를 한눈에 보이게 정리한다.

보고서 내용은 build_data() 로 한 번 정리하고, 두 가지로 그린다.
- render(): 패키지 폴더의 report.md (서버에서 cat 으로 봐도 읽히는 글)
- render_html(): ShipKit 보고서 창 (색 카드·표로 결과가 먼저 보이게)
정리한 데이터는 기록 파일(shipkit-record.json)에도 담아서, 나중에 기록 화면에서 같은 모양으로 다시 본다.

순서는 '무엇이 됐나 → 확인할 것 → 할 일 → 근거(MR·파일) → 서버 반영 방법'.
"""
from __future__ import annotations

import html
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from shipkit.models import Status

if TYPE_CHECKING:
    from shipkit.packager import PackageResult

STATUS_KEY = {Status.OK: "ok", Status.WARN: "warn", Status.ERROR: "error", Status.SKIPPED: "skipped"}
STATUS_TEXT = {"ok": "성공", "warn": "경고", "error": "실패", "skipped": "제외"}
_ICON = {"ok": "✅", "warn": "⚠️", "error": "❌", "skipped": "➖"}
HOOK_LABELS = {"pre_apply": "반영 전", "post_apply": "반영 후", "post_rollback": "되돌린 후"}
# sync 모드처럼 파일이 많으면 파일 목록 대신 최상위 폴더별 개수를 보여 준다
TOP_FOLDERS = 12


# ---------------------------------------------------------------------- 데이터
def build_data(result: "PackageResult") -> Dict[str, Any]:
    p, source = result.project, result.source
    sync = p.mode == "sync"
    entries = result.entries
    counts = {key: result.count(status) for status, key in STATUS_KEY.items()}
    problems = [{"status": STATUS_KEY[e.status], "path": e.change.path, "messages": list(e.messages)}
                for e in sorted(entries, key=lambda e: (e.status != Status.ERROR, e.change.path))
                if e.status in (Status.ERROR, Status.WARN)]
    files, skipped = [], []
    if not sync:
        for e in entries:
            if e.status == Status.SKIPPED:
                skipped.append({"path": e.change.path, "reason": "; ".join(e.messages)})
                continue
            files.append({
                "status": STATUS_KEY[e.status], "change": e.change.change.label, "path": e.change.path,
                "targets": [a.target for a in e.artifacts], "deletes": list(e.deletes),
                # 경고·실패 사유는 '확인 필요'에 따로 모으므로 여기에는 참고 메시지만
                "notes": list(e.messages) if e.status == Status.OK else [],
            })
    name = result.out_dir.name if result.out_dir else "<패키지 폴더>"
    upload, commands, rollback = server_steps(result)
    tips = []
    if p.mode == "sync" and p.prune:
        tips.append(f"옛 빌드 파일 정리: 서버의 {', '.join(p.prune)} 폴더에서 이번 빌드에 없는 파일은 apply 때 지웁니다 "
                    "(check 에서 미리 보이고, rollback 으로 되살아남).")
    if p.server_os == "windows":
        tips.append("인터넷·메일로 받은 패키지라 실행이 막히면 PowerShell 에서: Get-ChildItem -Recurse | Unblock-File")
    data: Dict[str, Any] = {
        "project": p.name,
        "created_at": f"{result.created_at:%Y-%m-%d %H:%M}",
        "package": name,
        "server_path": p.server_path,
        "server_os": p.server_os,
        "mode": p.mode,
        "mode_text": p.describe_mode,
        "source": source.description,
        "request_label": source.request_label,
        "requests": [{k: mr.get(k, "") for k in ("ref", "title", "author", "web_url", "source_branch", "target_branch")}
                     for mr in source.merge_requests],
        "counts": counts,
        "applied": len(result.manifest),
        "deletes": len(result.deletes),
        "warnings": list(source.warnings),
        "notes": list(source.notes),
        "hooks": dict(result.hooks),
        "problems": problems,
        "files": files,
        "skipped": skipped,
        "delete_list": list(result.deletes),
        "steps": {"upload": upload, "commands": [list(c) for c in commands], "rollback": list(rollback)},
        "tips": tips,
    }
    if sync:
        top = Counter(path.split("/")[0] if "/" in path else "(최상위)" for path in result.manifest)
        data["sync"] = {"build_files": len(result.manifest), "prune": list(p.prune),
                        "top": [[k, v] for k, v in sorted(top.items(), key=lambda kv: -kv[1])[:TOP_FOLDERS]],
                        "source_changes": [f"{c.change.label} {c.path}" for c in result.source_changes]}
    return data


def verdict(data: Dict[str, Any]) -> Tuple[str, str]:
    """결과 한 줄: (tone, 글). tone = success / warning / danger"""
    c = data["counts"]
    attention = len(data["warnings"]) + c.get("warn", 0)
    if c.get("error"):
        return "danger", f"실패 {c['error']}개는 패키지에서 빠졌습니다" + (f" · 확인할 항목 {attention}개" if attention else "")
    if attention:
        return "warning", f"확인할 항목 {attention}개가 있습니다. 아래 '확인 필요'를 보고 배포하세요"
    return "success", "문제 없이 만들어졌습니다"


# ---------------------------------------------------------------------- report.md
def _cell(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def render(result: "PackageResult") -> str:
    return render_markdown(build_data(result))


def render_markdown(d: Dict[str, Any]) -> str:
    lines: List[str] = []
    w = lines.append
    tone, text = verdict(d)
    c = d["counts"]
    w(f"# {d['project']} 배포 패키지")
    w("")
    w(f"{d['created_at']} · 패키지 `{d['package']}` · 서버 `{d['server_path']}`")
    w("")
    mark = {"success": "✅", "warning": "⚠️", "danger": "❌"}[tone]
    w(f"**{mark} {text}**")
    w("")
    if d.get("sync"):
        s = d["sync"]
        w("| 빌드 결과 파일 | ✅ 성공 | ⚠️ 경고 | ❌ 실패 |")
        w("|:---:|:---:|:---:|:---:|")
        w(f"| **{s['build_files']}** | {c['ok']} | {c['warn']} | {c['error']} |")
    else:
        w("| 서버 반영 | 서버 삭제 | ✅ 성공 | ⚠️ 경고 | ❌ 실패 | ➖ 제외 |")
        w("|:---:|:---:|:---:|:---:|:---:|:---:|")
        w(f"| **{d['applied']}** | **{d['deletes']}** | {c['ok']} | {c['warn']} | {c['error']} | {c['skipped']} |")
    w("")
    w(f"- 변경 출처: {d['source']}")
    w(f"- 배포 방식: {d['mode_text']}")
    w("")

    if d["warnings"] or d["problems"]:
        w("## ⚠️ 확인 필요")
        w("")
        for msg in d["warnings"]:
            w(f"- {msg}")
        for prob in d["problems"]:
            w(f"- {_ICON[prob['status']]} `{prob['path']}` — {'; '.join(prob['messages'])}")
        w("")
    if d["notes"]:
        w("## 📌 따로 할 일")
        w("")
        for msg in d["notes"]:
            w(f"- {msg}")
        w("")
    if d["hooks"]:
        w("## 서버에서 실행할 명령")
        w("")
        w("apply·rollback 때 실행 전에 묻습니다.")
        w("")
        for stage, commands in d["hooks"].items():
            for cmd in commands:
                w(f"- {HOOK_LABELS.get(stage, stage)}: `{cmd}`")
        w("")

    if d["requests"]:
        w(f"## {d['request_label']}")
        w("")
        for mr in d["requests"]:
            link = f"[{mr['ref']}]({mr['web_url']})" if mr.get("web_url") else mr["ref"]
            branch = f" · `{mr['source_branch']} → {mr['target_branch']}`" if mr.get("target_branch") else ""
            w(f"- {link} {mr['title']} · {mr['author']}{branch}")
        w("")

    if d.get("sync"):
        s = d["sync"]
        w(f"## 빌드 결과 ({s['build_files']}개 파일)")
        w("")
        w("서버와 내용이 다른 파일만 반영합니다.")
        w("")
        for folder, n in s["top"]:
            w(f"- `{folder}` {n}개")
        w("")
        if s["source_changes"]:
            w(f"## 소스 변경 ({len(s['source_changes'])}개)")
            w("")
            for line in s["source_changes"]:
                w(f"- {line}")
            w("")
    elif d["files"]:
        w(f"## 반영할 파일 ({len(d['files'])}개)")
        w("")
        w("| | 소스 파일 | 서버에 들어갈 파일 |")
        w("|:---:|:---|:---|")
        for f in d["files"]:
            out = [f"`{_cell(t)}`" for t in f["targets"]] + [f"(삭제) `{_cell(t)}`" for t in f["deletes"]]
            if f["status"] == "error":
                out = ["(빠짐)"]
            out += [_cell(n) for n in f["notes"]]
            w(f"| {_ICON[f['status']]} | `{_cell(f['path'])}` | {'<br>'.join(out) or '-'} |")
        w("")
    if d["skipped"]:
        w(f"## 제외된 파일 ({len(d['skipped'])}개)")
        w("")
        for s in d["skipped"]:
            w(f"- `{s['path']}` — {s['reason']}")
        w("")

    st = d["steps"]
    w("## 서버에 반영하는 방법")
    w("")
    w(f"1. {st['upload']}")
    w("2. 서버에서 순서대로 실행합니다.")
    w("")
    w("```powershell" if d["server_os"] == "windows" else "```bash")
    width = max(len(cmd) for cmd, _ in st["commands"]) + 2
    for cmd, desc in st["commands"]:
        w(f"{cmd:<{width}}# {desc}")
    w("```")
    w("")
    w(f"문제가 생기면 `{st['rollback'][0]}` 로 apply 이전으로 되돌립니다.")
    for tip in d["tips"]:
        w("")
        w(f"- {tip}")
    w("")
    return "\n".join(lines)


def write_report(path: Path, result: "PackageResult") -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(render(result))


# ---------------------------------------------------------------------- 보고서 창 (Qt 서식 HTML)
# Qt 의 서식 문서가 이해하는 범위(표·글자색·배경·여백)만 쓴다. 모서리 둥글기·flex 는 안 된다.
_T = {"text": "#2F2C25", "muted": "#7C7668", "border": "#E5E1D5", "head": "#F6F4EC", "code": "#EEEAE0",
      "success": ("#4F7A4A", "#EAF1E6"), "warning": ("#A3692B", "#F8EEDD"), "danger": ("#B4533E", "#F7E6E1"),
      "neutral": ("#4E493F", "#EEEAE0"), "primary": ("#7D7660", "#ECE8DB")}  # 화면에서는 theme.report_colors() 가 대신 준다
_TONE = {"ok": "success", "warn": "warning", "error": "danger", "skipped": "neutral"}
_MONO = "'Cascadia Mono', Consolas, 'Malgun Gothic'"


def _e(text: Any) -> str:
    return html.escape(str(text))


def _code(text: str) -> str:
    return f"<span style=\"font-family:{_MONO}; background:{_T['code']};\">&nbsp;{_e(text)}&nbsp;</span>"


def _badge(status: str) -> str:
    fg, bg = _T[_TONE[status]]
    return (f"<span style=\"color:{fg}; background:{bg}; font-weight:600;\">&nbsp;{STATUS_TEXT[status]}&nbsp;</span>")


def _h2(text: str, count: Optional[int] = None) -> str:
    extra = f"&nbsp;&nbsp;<span style=\"color:{_T['muted']}; font-weight:400;\">{count}</span>" if count is not None else ""
    return f"<h2 style=\"font-size:13pt; margin-top:26px; margin-bottom:10px;\">{_e(text)}{extra}</h2>"


def _list(items: List[str], color: str = "") -> str:
    style = f" style=\"color:{color};\"" if color else ""
    rows = "".join(f"<li style=\"margin-bottom:6px;\"{style}>{item}</li>" for item in items)
    return f"<ul style=\"margin-top:0; margin-left:-12px;\">{rows}</ul>"


def _table(head: List[str], rows: List[List[str]], widths: Optional[List[str]] = None) -> str:
    cell = f"padding:7px 10px; border-bottom:1px solid {_T['border']};"
    ths = "".join(f"<th align=\"left\" style=\"{cell} background:{_T['head']}; color:{_T['muted']}; font-size:9pt;\""
                  + (f" width=\"{widths[i]}\"" if widths and widths[i] else "") + f">{_e(h)}</th>"
                  for i, h in enumerate(head))
    trs = "".join("<tr>" + "".join(f"<td valign=\"top\" style=\"{cell}\">{c}</td>" for c in r) + "</tr>" for r in rows)
    return (f"<table width=\"100%\" cellspacing=\"0\" cellpadding=\"0\" style=\"border-collapse:collapse;\">"
            f"<tr>{ths}</tr>{trs}</table>")


def render_html(d: Dict[str, Any], colors: Optional[Dict[str, Any]] = None) -> str:
    """colors: 화면 테마 색 (theme.report_colors). 없으면 밝은 색."""
    global _T
    saved, _T = _T, {**_T, **(colors or {})}
    try:
        return _render_html(d)
    finally:
        _T = saved


def _render_html(d: Dict[str, Any]) -> str:
    c = d["counts"]
    tone, text = verdict(d)
    out: List[str] = []
    w = out.append
    w(f"<body style=\"color:{_T['text']}; font-size:10pt;\">")
    w(f"<h1 style=\"font-size:18pt; margin-bottom:4px;\">{_e(d['project'])} 배포 패키지</h1>")
    w(f"<p style=\"color:{_T['muted']}; margin-top:0;\">{_e(d['created_at'])}&nbsp;&nbsp;·&nbsp;&nbsp;"
      f"{_e(d['source'])}&nbsp;&nbsp;·&nbsp;&nbsp;서버 {_e(d['server_path'])}</p>")

    # 결과 한 줄
    fg, bg = _T[tone]
    mark = {"success": "✓", "warning": "!", "danger": "✕"}[tone]
    w(f"<table width=\"100%\" cellspacing=\"0\" cellpadding=\"12\" style=\"margin-top:14px;\">"
      f"<tr><td style=\"background:{bg}; color:{fg}; font-size:11.5pt; font-weight:600; "
      f"border:1px solid {fg};\">{mark}&nbsp;&nbsp;{_e(text)}</td></tr></table>")

    # 숫자 카드
    if d.get("sync"):
        cards = [("빌드 결과 파일", d["sync"]["build_files"], "primary")]
    else:
        cards = [("서버 반영", d["applied"], "primary"), ("서버 삭제", d["deletes"], "primary")]
    cards += [("성공", c["ok"], "success"), ("경고", c["warn"], "warning"), ("실패", c["error"], "danger")]
    if not d.get("sync"):
        cards.append(("제외", c["skipped"], "neutral"))
    cells = []
    for title, n, card_tone in cards:
        cfg, cbg = _T[card_tone]
        dim = not n and card_tone in ("warning", "danger")
        color = _T["muted"] if dim else cfg
        back = _T["head"] if dim else cbg
        cells.append(f"<td width=\"{100 // len(cards)}%\" style=\"background:{back}; padding:12px 14px;\">"
                     f"<div style=\"color:{color}; font-size:9pt;\">{title}</div>"
                     f"<div style=\"color:{color}; font-size:20pt; font-weight:700;\">{n}</div></td>")
    gap = "<td width=\"8\"></td>"
    w(f"<table width=\"100%\" cellspacing=\"0\" cellpadding=\"0\" style=\"margin-top:12px;\"><tr>{gap.join(cells)}</tr></table>")
    w(f"<p style=\"color:{_T['muted']}; margin-top:10px;\">{_e(d['mode_text'])}&nbsp;&nbsp;·&nbsp;&nbsp;"
      f"패키지 {_e(d['package'])}</p>")

    if d["warnings"] or d["problems"]:
        w(_h2("확인 필요", len(d["warnings"]) + len(d["problems"])))
        items = [_e(m) for m in d["warnings"]]
        items += [f"{_badge(p['status'])}&nbsp;&nbsp;{_code(p['path'])}<br>"
                  f"<span style=\"color:{_T['muted']};\">{_e('; '.join(p['messages']))}</span>" for p in d["problems"]]
        w(_list(items))
    todo = [_e(m) for m in d["notes"]]
    todo += [f"서버에서 {HOOK_LABELS.get(stage, stage)} 실행 (묻고 실행): {_code(cmd)}"
             for stage, cmds in d["hooks"].items() for cmd in cmds]
    if todo:
        w(_h2("따로 할 일"))
        w(_list(todo))

    if d["requests"]:
        w(_h2(d["request_label"], len(d["requests"])))
        rows = []
        for mr in d["requests"]:
            ref = (f"<a href=\"{_e(mr['web_url'])}\" style=\"color:{_T['primary'][0]}; font-weight:600; "
                   f"text-decoration:none;\">{_e(mr['ref'])}</a>") if mr.get("web_url") else _e(mr["ref"])
            branch = f"{_e(mr.get('source_branch', ''))} → <b>{_e(mr.get('target_branch', ''))}</b>" \
                if mr.get("target_branch") else ""
            rows.append([ref, _e(mr["title"]), _e(mr["author"]),
                         f"<span style=\"color:{_T['muted']};\">{branch}</span>"])
        w(_table(["번호", "제목", "작성자", "브랜치 (→ 머지 대상)"], rows, ["60", "", "90", "200"]))

    if d.get("sync"):
        s = d["sync"]
        w(_h2("빌드 결과", s["build_files"]))
        w(f"<p style=\"color:{_T['muted']};\">서버와 내용이 다른 파일만 반영합니다."
          + (f" 정리 폴더: {_e(', '.join(s['prune']))}" if s["prune"] else "") + "</p>")
        w(_table(["폴더", "파일 수"], [[_code(k), str(n)] for k, n in s["top"]], ["", "80"]))
        if s["source_changes"]:
            w(_h2("소스 변경", len(s["source_changes"])))
            w(_list([_e(x) for x in s["source_changes"]]))
    elif d["files"]:
        w(_h2("반영할 파일", len(d["files"])))
        rows = []
        for f in d["files"]:
            if f["status"] == "error":
                outs = [f"<span style=\"color:{_T['danger'][0]};\">패키지에서 빠짐</span>"]
            else:
                outs = [f"→&nbsp;{_e(t)}" for t in f["targets"]]
                outs += [f"<span style=\"color:{_T['danger'][0]};\">삭제&nbsp;{_e(t)}</span>" for t in f["deletes"]]
                outs += [f"<span style=\"color:{_T['muted']};\">{_e(n)}</span>" for n in f["notes"]]
            rows.append([_badge(f["status"]),
                         f"<span style=\"color:{_T['muted']};\">{_e(f['change'])}</span>&nbsp;&nbsp;{_e(f['path'])}",
                         "<br>".join(outs) or "-"])
        w(_table(["상태", "소스 파일", "서버에 들어갈 파일"], rows, ["56", "45%", ""]))
    if d["skipped"]:
        w(_h2("제외된 파일", len(d["skipped"])))
        w(_list([f"{_e(s['path'])}&nbsp;&nbsp;<span style=\"color:{_T['muted']};\">{_e(s['reason'])}</span>"
                 for s in d["skipped"]]))

    st = d["steps"]
    w(_h2("서버에 반영하는 방법"))
    w(f"<p style=\"margin-bottom:8px;\">1.&nbsp;&nbsp;{_e(st['upload'])}<br>2.&nbsp;&nbsp;서버에서 순서대로 실행합니다.</p>")
    rows = [[_code(cmd), f"<span style=\"color:{_T['muted']};\">{_e(desc)}</span>"] for cmd, desc in st["commands"]]
    rows.append([_code(st["rollback"][0]), f"<span style=\"color:{_T['danger'][0]};\">{_e(st['rollback'][1])}</span>"])
    w(_table(["명령", "하는 일"], rows, ["", "45%"]))
    if d["tips"]:
        w(_list([_e(t) for t in d["tips"]], _T["muted"]))
    w("</body>")
    return "".join(out)


# ---------------------------------------------------------------------- 서버 반영 절차 (화면·보고서 공통)
def server_steps(result: "PackageResult") -> Tuple[str, List[Tuple[str, str]], Tuple[str, str]]:
    """서버 반영 절차: (올리기 안내, [(명령, 설명)...], (원복 명령, 설명))."""
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
