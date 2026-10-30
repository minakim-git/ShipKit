"""기록 화면: 이 프로젝트로 만든 패키지 목록(왼쪽)과 고른 패키지의 내용(오른쪽).

"지난주에 뭘 올렸지?", "이 파일 언제 배포했지?", "어느 걸 되돌려야 하지?"를 찾는 곳.
"""
from __future__ import annotations

from typing import List, Optional

from PySide6.QtCore import QSize, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QIcon
from PySide6.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QLineEdit, QListWidget, QListWidgetItem, QStackedWidget, QTabWidget,
    QVBoxLayout, QWidget,
)

from shipkit import history, report
from shipkit.gui import theme
from shipkit.gui.context import AppContext
from shipkit.gui.remote_panel import RemoteClient
from shipkit.gui.report_view import show_report
from shipkit.gui.server_status import (
    ACTION_TEXT, RemoteRunDialog, ServerStatusDialog, _profile_by_name, ask_rollback_hooks,
)
from shipkit.gui.widgets import Card, Chip, CommandRow, button, label, open_path

SOURCE_LABEL = {"gitlab-mr": "GitLab MR", "github-pr": "GitHub PR", "git-diff": "git 범위", "manual": "파일 목록",
                "build": "빌드 결과", "unknown": "예전 패키지"}


def _clear(layout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        if item.widget():
            item.widget().hide()
            item.widget().deleteLater()


class RecordRow(QWidget):
    """목록 한 줄: 시각 · 출처 배지 · 파일 수 / 아래에 설명."""

    def __init__(self, record: history.PackageRecord):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 9, 12, 9)
        layout.setSpacing(4)
        top = QHBoxLayout()
        top.setSpacing(6)
        top.addWidget(label(f"{record.created_at:%Y-%m-%d  %H:%M}", "rowTitle", wrap=False))
        top.addStretch(1)
        if record.counts.get("error"):
            top.addWidget(Chip(f"실패 {record.counts['error']}", "danger"))
        top.addWidget(Chip(f"파일 {len(record.applied)}" + (f" · 삭제 {len(record.deletes)}" if record.deletes else ""),
                           "neutral"))
        layout.addLayout(top)
        desc = label(_title(record), "muted", wrap=False)
        desc.setToolTip(record.description)
        layout.addWidget(desc)


def _title(record: history.PackageRecord) -> str:
    """목록·상세 제목: MR 이면 제목까지, 아니면 출처 설명."""
    if record.requests:
        first = record.requests[0]
        more = f" 외 {len(record.requests) - 1}개" if len(record.requests) > 1 else ""
        return f"{first.get('ref', '')}  {first.get('title') or ''}{more}".strip()
    return record.description


class HistoryPage(QWidget):
    remake_requested = Signal(object)  # PackageRecord: 같은 내용으로 다시 만들기

    def __init__(self, ctx: AppContext):
        super().__init__()
        self.ctx = ctx
        self.records: List[history.PackageRecord] = []
        self.current: Optional[history.PackageRecord] = None
        self.client = RemoteClient(ctx)      # 서버 상태 조회·서버에서 되돌리기 (지문·비밀번호는 한 번만 묻게 공유)
        self.status_dialog: Optional[ServerStatusDialog] = None
        self.rollback_dialog: Optional[RemoteRunDialog] = None
        self._build_ui()
        ctx.project_listeners.append(self.refresh)

    # ------------------------------------------------------------------ 화면 구성
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(14)
        head = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(2)
        titles.addWidget(label("기록", "pageTitle"))
        self.subtitle = label("", "pageSubtitle", wrap=False)
        titles.addWidget(self.subtitle)
        head.addLayout(titles, 1)
        self.open_base_btn = button("패키지 폴더 열기", "ghost")
        self.open_base_btn.clicked.connect(lambda: self.ctx.project and open_path(self.ctx.project.output_dir))
        refresh = button("새로 고침", "ghost")
        theme.bind_icon(refresh, "refresh.svg", "PRIMARY", 16)
        refresh.clicked.connect(self.refresh)
        self.status_btn = button("서버 상태", "ghost")   # 서버 연결을 등록한 프로젝트만
        self.status_btn.setToolTip("서버의 배포 이력을 읽어, 지금 어떤 패키지가 반영돼 있는지 보여 줍니다.")
        self.status_btn.clicked.connect(self.show_server_status)
        head.addWidget(self.status_btn, 0, Qt.AlignmentFlag.AlignBottom)
        head.addWidget(self.open_base_btn, 0, Qt.AlignmentFlag.AlignBottom)
        head.addWidget(refresh, 0, Qt.AlignmentFlag.AlignBottom)
        root.addLayout(head)

        self.body = QStackedWidget()
        self.body.addWidget(self._main())
        self.empty = label("", "emptyText")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.body.addWidget(self.empty)
        root.addWidget(self.body, 1)

    def _main(self) -> QWidget:
        w = QWidget()
        row = QHBoxLayout(w)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(14)

        left = Card(padding=12)
        left.setFixedWidth(360)
        self.search = QLineEdit()
        self.search.setPlaceholderText("MR 번호·제목·파일 경로로 찾기")
        search_icon = self.search.addAction(QIcon(), QLineEdit.ActionPosition.LeadingPosition)
        theme.bind_icon(search_icon, "search.svg", "SUBTLE", 16)
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._apply_search)
        left.body.addWidget(self.search)
        self.list = QListWidget()
        self.list.setObjectName("records")
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.list.currentRowChanged.connect(self._on_select)
        left.body.addWidget(self.list, 1)
        self.count_label = label("", "muted", wrap=False)
        left.body.addWidget(self.count_label)
        row.addWidget(left)

        right = Card(padding=20)
        head = QHBoxLayout()
        texts = QVBoxLayout()
        texts.setSpacing(3)
        self.detail_title = label("", "detailTitle")
        self.detail_title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.detail_sub = label("", "muted")
        self.detail_sub.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        texts.addWidget(self.detail_title)
        texts.addWidget(self.detail_sub)
        head.addLayout(texts, 1)
        right.body.addLayout(head)
        self.chips = QHBoxLayout()
        self.chips.setSpacing(6)
        right.body.addLayout(self.chips)
        self.requests_box = QVBoxLayout()
        self.requests_box.setSpacing(4)
        right.body.addLayout(self.requests_box)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.files = QListWidget()
        self.files.setObjectName("fileList")
        self.files.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.files.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.tabs.addTab(self.files, "파일")
        commands = QWidget()
        self.command_box = QVBoxLayout(commands)
        self.command_box.setContentsMargins(0, 4, 0, 0)
        self.command_box.setSpacing(6)
        self.tabs.addTab(commands, "서버 명령")
        self.deploy_list = QListWidget()   # 화면에서 서버에 올리고 실행한 이력
        self.deploy_list.setObjectName("fileList")
        self.deploy_list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.deploy_list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.tabs.addTab(self.deploy_list, "서버 반영")
        right.body.addWidget(self.tabs, 1)

        bar = QHBoxLayout()
        bar.setSpacing(10)
        self.open_btn = button("폴더 열기")
        self.open_btn.clicked.connect(lambda: self.current and open_path(self.current.path))
        self.report_btn = button("보고서 열기")
        self.report_btn.clicked.connect(lambda: self.current and show_report(self, self.current.report))
        self.rollback_btn = button("서버에서 되돌리기", "danger")
        self.rollback_btn.clicked.connect(self.rollback_on_server)
        self.remake_btn = button("같은 내용으로 다시 만들기", "primary")
        self.remake_btn.setToolTip("배포 화면에 같은 MR·파일을 채워 넣습니다. 지금 로컬 파일로 새 패키지를 만듭니다.")
        self.remake_btn.clicked.connect(lambda: self.current and self.remake_requested.emit(self.current))
        bar.addWidget(self.open_btn)
        bar.addWidget(self.report_btn)
        bar.addWidget(self.rollback_btn)
        bar.addStretch(1)
        bar.addWidget(self.remake_btn)
        right.body.addLayout(bar)
        row.addWidget(right, 1)
        return w

    # ------------------------------------------------------------------ 내용
    def refresh(self) -> None:
        p = self.ctx.project
        selected = self.current.path if self.current else None
        self.records = history.list_records(p) if p else []
        self.subtitle.setText(f"{p.name} 프로젝트로 만든 패키지 (최근 것부터)" if p else "")
        self.status_btn.setVisible(bool(p and p.servers))
        self.open_base_btn.setVisible(p is not None)
        self.open_base_btn.setToolTip(str(p.output_dir) if p else "")
        if not self.records:
            self.current = None
            self.empty.setText("아직 만든 패키지가 없습니다.\n배포 화면에서 패키지를 만들면 여기에 쌓입니다." if p else
                               "왼쪽 위에서 프로젝트를 고르세요.")
            self.body.setCurrentIndex(1)
            return
        self.body.setCurrentIndex(0)
        self.list.blockSignals(True)
        self.list.clear()
        for record in self.records:
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, record.search_text())
            row = RecordRow(record)
            item.setSizeHint(QSize(0, row.sizeHint().height()))
            self.list.addItem(item)
            self.list.setItemWidget(item, row)
        self.list.blockSignals(False)
        index = next((i for i, r in enumerate(self.records) if r.path == selected), 0)
        self._apply_search()
        self.list.setCurrentRow(index)
        self._on_select(index)

    def _apply_search(self) -> None:
        words = self.search.text().lower().split()
        shown = 0
        for i in range(self.list.count()):
            item = self.list.item(i)
            hit = all(w in item.data(Qt.ItemDataRole.UserRole) for w in words)
            item.setHidden(not hit)
            shown += hit
        total = len(self.records)
        self.count_label.setText(f"{shown}개 찾음 / 전체 {total}개" if words else
                                 f"전체 {total}개" + ("  (최근 것만 표시)" if total >= history.LIST_LIMIT else ""))

    def _on_select(self, row: int) -> None:
        if not 0 <= row < len(self.records):
            return
        r = self.current = self.records[row]
        self.detail_title.setText(_title(r))
        when = f"{r.created_at:%Y-%m-%d %H:%M:%S}"
        self.detail_sub.setText(f"{r.name}  ·  {when}  ·  {SOURCE_LABEL.get(r.source_kind, r.source_kind)}"
                                + (f"  ·  서버 경로 {r.server_path}" if r.server_path else ""))

        _clear(self.chips)
        facts = [(f"서버 반영 {len(r.applied)}개", "primary")]
        if r.deletes:
            facts.append((f"서버 삭제 {len(r.deletes)}개", "neutral"))
        if r.counts.get("warn"):
            facts.append((f"경고 {r.counts['warn']}", "warning"))
        if r.counts.get("error"):
            facts.append((f"실패 {r.counts['error']}개는 빠짐", "danger"))
        if r.archive:
            facts.append((f"압축 {r.archive.name}", "neutral"))
        if not r.has_record:
            facts.append(("예전 패키지 (보고서에서 읽음)", "neutral"))
        for server, d in r.server_states().items():  # 서버마다 마지막 상태: 반영됨 / 되돌림 / 실패
            when = str(d.get("at", ""))[5:16].replace("T", " ")
            if not d.get("ok"):
                facts.append((f"{server}  {ACTION_TEXT.get(d.get('action'), d.get('action'))} 실패  {when}", "danger"))
            elif d.get("action") == "apply":
                facts.append((f"{server}  반영됨  {when}", "success"))
            else:
                facts.append((f"{server}  되돌림  {when}", "warning"))
        for text, tone in facts:
            self.chips.addWidget(Chip(text, tone))
        self.chips.addStretch(1)

        _clear(self.requests_box)  # MR/PR 마다 한 줄 + 원본 열기
        for req in r.requests if len(r.requests) > 1 or (r.requests and r.requests[0].get("web_url")) else []:
            line = QHBoxLayout()
            line.setSpacing(10)
            holder = QWidget()
            holder.setLayout(line)
            line.setContentsMargins(0, 0, 0, 0)
            ref = label(req.get("ref", ""), "refLabel", wrap=False)
            line.addWidget(ref)
            line.addWidget(label(req.get("title", ""), wrap=False), 1)
            if req.get("author"):
                line.addWidget(label(str(req["author"]), "muted", wrap=False))
            if req.get("web_url"):
                link = button("열기", "ghost")
                theme.bind_icon(link, "external.svg", "PRIMARY", 14)
                link.setToolTip(req["web_url"])
                link.clicked.connect(lambda _=False, url=req["web_url"]: QDesktopServices.openUrl(QUrl(url)))
                line.addWidget(link)
            self.requests_box.addWidget(holder)

        self.files.clear()
        for path in r.applied:
            self.files.addItem(path)
        for path in r.deletes:
            item = QListWidgetItem(f"(삭제) {path}")
            item.setForeground(theme.tone_color("danger"))
            self.files.addItem(item)
        self.tabs.setTabText(0, f"파일 {len(r.applied) + len(r.deletes)}")

        _clear(self.command_box)
        upload, steps, (rollback, rollback_desc) = report.server_steps_for(
            r.name, r.server_path or "서버 배포 경로", r.server_os, r.script_name, r.archive_kind)
        self.command_box.addWidget(label(upload, "muted"))
        for i, (cmd, desc) in enumerate(steps, 1):
            self.command_box.addWidget(CommandRow(str(i), cmd, desc))
        self.command_box.addWidget(CommandRow("↺", rollback, rollback_desc))
        self.command_box.addStretch(1)
        self.deploy_list.clear()
        for d in reversed(r.deployments):
            when = str(d.get("at", "")).replace("T", " ")
            text = (f"{when}  {d.get('server', '')} ({d.get('address', '')})  "
                    f"{ACTION_TEXT.get(d.get('action'), d.get('action'))}  "
                    f"{'성공' if d.get('ok') else f'실패 (종료 코드 {d.get('code')})'}")
            item = QListWidgetItem(text)
            if not d.get("ok"):
                item.setForeground(theme.tone_color("danger"))
            self.deploy_list.addItem(item)
        self.tabs.setTabText(2, f"서버 반영 {len(r.deployments)}" if r.deployments else "서버 반영")
        can_remake = bool(r.changes or r.requests or r.source_kind == "build")
        self.remake_btn.setEnabled(can_remake)
        self.report_btn.setEnabled(r.report.is_file())
        targets = self._rollback_targets(r)
        self.rollback_btn.setVisible(bool(r.deployments))
        self.rollback_btn.setEnabled(bool(targets))
        self.rollback_btn.setToolTip("이 패키지가 반영돼 있는 서버에서 shipkit.sh rollback 을 실행합니다: "
                                     + ", ".join(t[0].name for t in targets) if targets else
                                     "이 패키지가 지금 반영돼 있는 서버가 없습니다 (또는 서버 연결 설정에서 지워짐).")

    # ------------------------------------------------------------------ 서버
    def _rollback_targets(self, r: history.PackageRecord) -> List[tuple]:
        """(서버 연결, 이력): 이 패키지가 반영돼 있고 서버 연결이 아직 설정에 있는 것."""
        p = self.ctx.project
        if p is None:
            return []
        targets = []
        for d in r.applied_servers():
            profile = _profile_by_name(p, str(d.get("server", "")))
            if profile is not None:
                targets.append((profile, d))
        return targets

    def show_server_status(self) -> None:
        p = self.ctx.project
        if p is None or not p.servers:
            return
        if self.status_dialog is not None and self.status_dialog.isVisible():
            self.status_dialog.raise_()
            return
        self.status_dialog = ServerStatusDialog(self.ctx, p, self, client=self.client)
        self.status_dialog.show()

    def rollback_on_server(self) -> None:
        """[서버에서 되돌리기]: 이 패키지가 반영된 서버에서 rollback. 여러 서버면 하나를 고른다."""
        r, p = self.current, self.ctx.project
        if r is None or p is None:
            return
        targets = self._rollback_targets(r)
        if not targets:
            return
        profile, deployment = targets[0]
        if len(targets) > 1:
            names = [t[0].name for t in targets]
            choice = self.ctx.dialogs.choose("서버 고르기", "어느 서버에서 되돌릴까요?", names)
            if choice is None:
                return
            profile, deployment = targets[names.index(choice)]
        if not self.ctx.dialogs.ask("서버에서 되돌리기",
                                   f"{profile.name} 서버({profile.address})의 {deployment.get('target', '')} 를\n"
                                   f"패키지 {r.name} 을 반영하기 전으로 되돌립니다.\n\n계속할까요?"):
            return
        run_hooks = ask_rollback_hooks(self.ctx, r)
        self.rollback_dialog = RemoteRunDialog(self.ctx, r, p, profile, deployment, self, client=self.client,
                                               on_finished=self.refresh)
        self.rollback_dialog.start(run_hooks)
