"""기록 화면에서 쓰는 서버 창 두 가지.

- ServerStatusDialog: 서버의 배포 이력(history.log)을 읽어 "지금 어떤 패키지가 반영돼 있나"와 최근 이력을 보여 준다.
- RemoteRunDialog: 기록에서 고른 패키지를 서버에서 되돌릴 때 출력을 보여 주는 창.
둘 다 배포 완료 화면과 같은 RemoteClient(지문 확인·비밀번호·백그라운드 실행)를 쓴다.
"""
from __future__ import annotations

from typing import Callable, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (
    QComboBox, QDialog, QHBoxLayout, QHeaderView, QPlainTextEdit, QTableWidget, QTableWidgetItem, QVBoxLayout,
    QWidget,
)

from shipkit import history, remote
from shipkit.config import ProjectConfig, ServerProfile
from shipkit.gui.context import AppContext
from shipkit.gui.remote_panel import HOOK_TEXT, RemoteClient
from shipkit.gui.widgets import Chip, button, label

ACTION_TEXT = {"check": "확인", "backup": "백업", "apply": "반영", "rollback": "되돌림"}


def _profile_by_name(project: ProjectConfig, name: str) -> Optional[ServerProfile]:
    return next((s for s in project.servers if s.name == name), None)


class ServerStatusDialog(QDialog):
    """서버 상태: 서버를 고르고 [조회]를 누르면 이력 파일을 읽어 온다."""

    def __init__(self, ctx: AppContext, project: ProjectConfig, parent: Optional[QWidget] = None,
                 client: Optional[RemoteClient] = None):
        super().__init__(parent)
        self.ctx = ctx
        self.project = project
        self.client = client or RemoteClient(ctx)
        self.entries: List[dict] = []
        self.setWindowTitle(f"서버 상태 - {project.name}")
        self.resize(820, 520)
        self.setWindowModality(Qt.WindowModality.NonModal)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(10)
        head = QHBoxLayout()
        self.server_combo = QComboBox()
        for s in project.servers:
            self.server_combo.addItem(f"{s.name}  ·  {s.address}", s.name)
        head.addWidget(label("서버", "fieldLabel", wrap=False))
        head.addWidget(self.server_combo, 1)
        self.fetch_btn = button("조회", "primary")
        self.fetch_btn.clicked.connect(self.fetch)
        head.addWidget(self.fetch_btn)
        layout.addLayout(head)
        self.state = Chip("아직 조회하지 않음", "neutral")
        self.summary = label("", "cardTitle")
        self.summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        row = QHBoxLayout()
        row.addWidget(self.summary, 1)
        row.addWidget(self.state)
        layout.addLayout(row)
        self.note = label("", "muted")
        layout.addWidget(self.note)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["시각", "계정", "명령", "대상 경로", "패키지", "결과"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        layout.addWidget(self.table, 1)
        close = button("닫기")
        close.clicked.connect(self.close)
        foot = QHBoxLayout()
        foot.addStretch(1)
        foot.addWidget(close)
        layout.addLayout(foot)

    @property
    def profile(self) -> Optional[ServerProfile]:
        return _profile_by_name(self.project, self.server_combo.currentData())

    def fetch(self) -> None:
        p = self.profile
        if p is None:
            return
        self.fetch_btn.setEnabled(False)
        self.state.set("조회 중…", "info")
        history_file = self.project.server.history_file

        def make_step(secrets):
            return lambda conn, output, progress: remote.fetch_history(conn, p, secrets, history_file, output)

        def done(entries: List[dict]) -> None:
            self.fetch_btn.setEnabled(True)
            self.show_entries(p, entries)

        def failed(message: str) -> None:
            self.fetch_btn.setEnabled(True)
            self.state.set("조회 실패", "danger")
            self.ctx.dialogs.error("서버 상태", message)

        self.client.start(p, make_step, lambda _t: None, lambda _a, _b: None, done, failed,
                          lambda: (self.fetch_btn.setEnabled(True), self.state.set("조회하지 않음", "neutral")))

    def show_entries(self, p: ServerProfile, entries: List[dict]) -> None:
        self.entries = entries
        target = p.server_path or self.project.server_path
        current = remote.current_package(entries, target)
        if current is None:
            self.summary.setText(f"{target} 에 반영한 기록이 없습니다")
            self.state.set("이력 없음", "neutral")
        elif current["action"] == "apply":
            self.summary.setText(f"지금 반영된 패키지: {current['package']}")
            self.state.set(f"{current['at']} · {current['user']} 계정이 반영", "success")
        else:
            self.summary.setText(f"마지막 동작: {current['package']} 되돌림")
            self.state.set(f"{current['at']} · {current['user']} 계정이 되돌림", "warning")
        self.note.setText(f"{p.runner} 계정의 {self.project.server.history_file} 기준, 최근 것부터 {len(entries)}건")
        self.table.setRowCount(0)
        for e in reversed(entries):
            row = self.table.rowCount()
            self.table.insertRow(row)
            values = [e["at"], e["user"], e["action"], e["target"], e["package"], e["result"]]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                if col == 3 and value == target:
                    item.setToolTip("이 프로젝트의 서버 배포 경로")
                self.table.setItem(row, col, item)


class RemoteRunDialog(QDialog):
    """서버에서 되돌리기: 기록의 패키지 폴더(remote_dir)에서 shipkit.sh rollback 을 실행하고 출력을 보여 준다."""

    def __init__(self, ctx: AppContext, record: history.PackageRecord, project: ProjectConfig,
                 profile: ServerProfile, deployment: dict, parent: Optional[QWidget] = None,
                 client: Optional[RemoteClient] = None, on_finished: Optional[Callable[[], None]] = None):
        super().__init__(parent)
        self.ctx = ctx
        self.record = record
        self.project = project
        self.profile = profile
        self.deployment = deployment
        self.client = client or RemoteClient(ctx)
        self.on_finished = on_finished
        self.result: Optional[remote.StepResult] = None
        self.setWindowTitle(f"서버에서 되돌리기 - {record.name}")
        self.resize(760, 460)
        self.setWindowModality(Qt.WindowModality.NonModal)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(10)
        head = QHBoxLayout()
        head.addWidget(label(f"{profile.name} ({profile.address})  ·  {deployment.get('target', '')}", "cardTitle"), 1)
        self.state = Chip("진행 중…", "info")
        head.addWidget(self.state)
        layout.addLayout(head)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setObjectName("serverLog")
        self.log.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        layout.addWidget(self.log, 1)
        foot = QHBoxLayout()
        foot.addStretch(1)
        self.cancel_btn = button("취소")
        self.cancel_btn.clicked.connect(self._cancel)
        self.close_btn = button("닫기")
        self.close_btn.clicked.connect(self.close)
        self.close_btn.hide()
        foot.addWidget(self.cancel_btn)
        foot.addWidget(self.close_btn)
        layout.addLayout(foot)

    def _append(self, text: str) -> None:
        self.log.moveCursor(self.log.textCursor().MoveOperation.End)
        self.log.insertPlainText(text.replace("\r\n", "\n"))
        self.log.ensureCursorVisible()

    def _finish(self, text: str, tone: str) -> None:
        self.state.set(text, tone)
        self.cancel_btn.hide()
        self.close_btn.show()
        if self.on_finished:
            self.on_finished()

    def _cancel(self) -> None:
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.setText("중단 중…")
        self.client.cancel()

    def start(self, run_hooks: bool) -> None:
        self.show()
        p, remote_dir = self.profile, str(self.deployment.get("remote_dir", ""))

        def make_step(secrets):
            return lambda conn, output, progress: remote.run_action(
                conn, p, secrets, remote_dir, "rollback", output, yes=run_hooks, no_hooks=not run_hooks)

        def done(res: remote.StepResult) -> None:
            self.result = res
            try:
                history.append_deployment(self.record.path, p.name, p.address, res.action, res.code, res.remote_dir,
                                          str(self.deployment.get("target", "")))
            except OSError as e:
                self._append(f"\n[경고] 반영 이력을 기록 파일에 남기지 못했습니다: {e}\n")
            self._append("\n✓ 되돌렸습니다.\n" if res.ok else f"\n✗ rollback 실패 (종료 코드 {res.code})\n")
            self._finish("되돌림" if res.ok else "되돌리기 실패", "warning" if res.ok else "danger")

        def failed(message: str) -> None:
            self._append(f"\n[오류] {message}\n")
            self._finish("실행 못 함", "danger")

        self.client.start(p, make_step, self._append, lambda _a, _b: None, done, failed,
                          lambda: self._finish("취소함", "neutral"))


def ask_rollback_hooks(ctx: AppContext, record: history.PackageRecord) -> bool:
    """post_rollback 명령이 있으면 함께 실행할지 묻는다 (없으면 True)."""
    commands = (record.hooks or {}).get("post_rollback") or []
    if not commands:
        return True
    lines = "\n".join(f"- {HOOK_TEXT['post_rollback']}: {cmd}" for cmd in commands)
    return ctx.dialogs.ask("서버 명령", f"이 패키지에는 되돌린 뒤 실행할 명령이 있습니다.\n\n{lines}\n\n"
                                     "함께 실행할까요? ([아니요]를 누르면 파일만 되돌립니다)")
