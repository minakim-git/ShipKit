"""여러 서버에 차례로 반영: 같은 패키지를 고른 서버들에 순서대로 올리고 확인 → 백업 후 반영 → (필요하면) 되돌리기.

배포 완료 화면의 [여러 서버에 차례로…] 로 연다. 서버마다 한 줄(상태·결과)이고, 출력은 아래 한 로그에 서버 머리줄로
나뉘어 쌓인다. 접속·지문·비밀번호는 완료 화면과 같은 RemoteClient 를 쓴다.
확인(check)은 한 서버가 실패해도 다음 서버로 넘어가고, 반영·되돌리기는 한 서버라도 실패하면 거기서 멈춘다(나머지 '중단').
패키지를 새로 만들기 전까지는 창을 닫았다 열어도 상태와 로그가 남는다.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QHBoxLayout, QHeaderView, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget

from shipkit import remote
from shipkit.config import ProjectConfig, ServerProfile
from shipkit.gui import theme
from shipkit.gui.context import AppContext
from shipkit.gui.remote_panel import (
    RemoteClient, ServerLog, ask_hooks, branch_mismatch, notify_messenger, record_deployment,
)
from shipkit.gui.widgets import Chip, button, label

STATE_TEXT = {
    "ready": ("아직 안 올림", "neutral"), "checking": ("확인 중…", "info"), "checked": ("확인 끝", "success"),
    "check_failed": ("확인 실패", "danger"), "applying": ("반영 중…", "info"), "applied": ("반영 완료", "success"),
    "apply_failed": ("반영 실패", "danger"), "rolling": ("되돌리는 중…", "info"), "rolled_back": ("되돌림", "warning"),
    "skipped": ("중단", "neutral"), "cancelled": ("취소함", "neutral"),
}
RUNNING = {"check": "checking", "apply": "applying", "rollback": "rolling"}
COL_NAME, COL_ADDR, COL_TARGET, COL_STATE, COL_NOTE = range(5)


class BatchDeployDialog(QDialog):
    """고른 서버들에 차례로: [올리고 확인] → [백업 후 반영] → [되돌리기]."""

    def __init__(self, ctx: AppContext, result, parent: Optional[QWidget] = None,
                 client: Optional[RemoteClient] = None, on_finished: Optional[Callable[[], None]] = None):
        super().__init__(parent)
        self.ctx = ctx
        self.result = result                       # 방금 만든 패키지 (PackageResult)
        self.project: ProjectConfig = result.project
        self.client = client or RemoteClient(ctx)
        self.on_finished = on_finished
        self.states: Dict[str, str] = {s.name: "ready" for s in self.project.servers}
        self.notes: Dict[str, str] = {}
        self.remote_dirs: Dict[str, str] = {}      # 서버별 올린 패키지 폴더 (apply·rollback 에 필요)
        self.checks: Dict[str, Optional[remote.CheckResult]] = {}
        self._queue: List[ServerProfile] = []
        self._action = ""
        self._run_hooks = True
        self._busy = False
        self._buffer: List[str] = []               # 지금 서버의 출력 (check 결과를 읽으려고)
        self.setWindowTitle(f"여러 서버에 차례로 반영 - {result.out_dir.name}")
        self.resize(900, 620)
        self.setWindowModality(Qt.WindowModality.NonModal)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(10)
        layout.addWidget(label(f"패키지 {result.out_dir.name}  ·  고른 서버에 위에서부터 차례로 진행합니다. "
                               "확인은 실패해도 다음 서버로 넘어가고, 반영·되돌리기는 한 서버라도 실패하면 멈춥니다.", "muted"))
        self.tree = QTreeWidget()
        self.tree.setObjectName("boxed")
        self.tree.setHeaderLabels(["서버", "주소", "반영 경로", "상태", "결과"])
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.header().setSectionResizeMode(COL_NOTE, QHeaderView.ResizeMode.Stretch)
        self.tree.setMaximumHeight(60 + 26 * max(2, len(self.project.servers)))
        for s in self.project.servers:
            item = QTreeWidgetItem([s.name + ("  (운영)" if s.is_production else ""), s.address,
                                    s.server_path or self.project.server_path, "", ""])
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(COL_NAME, Qt.CheckState.Checked)
            item.setData(COL_NAME, Qt.ItemDataRole.UserRole, s.name)
            if s.is_production:
                item.setForeground(COL_NAME, theme.tone_color("danger"))
            self.tree.addTopLevelItem(item)
        self.tree.itemChanged.connect(lambda *_a: self._update_buttons())
        layout.addWidget(self.tree)

        buttons = QHBoxLayout()
        buttons.setSpacing(6)
        self.check_btn = button("올리고 확인", "primary")
        self.check_btn.setToolTip("고른 서버마다 패키지를 올리고 check 를 실행합니다 (서버는 바뀌지 않음)")
        self.check_btn.clicked.connect(self.upload_and_check)
        self.apply_btn = button("백업 후 반영")
        self.apply_btn.setToolTip("확인이 끝난 서버마다 backup → apply")
        self.apply_btn.clicked.connect(self.apply)
        self.rollback_btn = button("되돌리기", "danger")
        self.rollback_btn.setToolTip("반영한 서버마다 rollback")
        self.rollback_btn.clicked.connect(self.rollback)
        self.state_chip = Chip("아직 올리지 않음", "neutral")
        self.cancel_btn = button("취소", "ghost")
        self.cancel_btn.setToolTip("지금 서버의 올리기·실행을 끊고 나머지 서버는 진행하지 않습니다.")
        self.cancel_btn.clicked.connect(self.cancel)
        self.cancel_btn.hide()
        self.close_btn = button("닫기")
        self.close_btn.clicked.connect(self.close)
        for b in (self.check_btn, self.apply_btn, self.rollback_btn):
            buttons.addWidget(b)
        buttons.addWidget(self.state_chip, 0, Qt.AlignmentFlag.AlignVCenter)
        buttons.addWidget(self.cancel_btn)
        buttons.addStretch(1)
        buttons.addWidget(self.close_btn)
        layout.addLayout(buttons)

        self.log = ServerLog()
        self.log.setMinimumHeight(200)
        layout.addWidget(self.log, 1)
        self._refresh_rows()
        self._update_buttons()

    # ------------------------------------------------------------------ 표·단추
    def _items(self) -> List[QTreeWidgetItem]:
        return [self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())]

    def selected(self) -> List[ServerProfile]:
        """체크한 서버 (표의 순서대로)."""
        names = [it.data(COL_NAME, Qt.ItemDataRole.UserRole) for it in self._items()
                 if it.checkState(COL_NAME) == Qt.CheckState.Checked]
        return [s for s in self.project.servers if s.name in names]

    def _refresh_rows(self) -> None:
        for it in self._items():
            name = it.data(COL_NAME, Qt.ItemDataRole.UserRole)
            text, tone = STATE_TEXT[self.states.get(name, "ready")]
            it.setText(COL_STATE, text)
            it.setForeground(COL_STATE, theme.tone_color(tone))
            it.setText(COL_NOTE, self.notes.get(name, ""))
        for col in (COL_NAME, COL_ADDR, COL_TARGET, COL_STATE):
            self.tree.resizeColumnToContents(col)

    def _set_state(self, name: str, state: str, note: Optional[str] = None) -> None:
        self.states[name] = state
        if note is not None:
            self.notes[name] = note
        self._refresh_rows()

    def _update_buttons(self) -> None:
        picked = [s.name for s in self.selected()]
        self.check_btn.setEnabled(not self._busy and bool(picked))
        self.apply_btn.setEnabled(not self._busy and any(self.states[n] == "checked" for n in picked))
        self.rollback_btn.setEnabled(not self._busy and any(self.states[n] in ("applied", "apply_failed") for n in picked))
        self.tree.setEnabled(not self._busy)
        self.cancel_btn.setVisible(self._busy)
        self.close_btn.setEnabled(not self._busy)

    # ------------------------------------------------------------------ 동작
    def upload_and_check(self) -> None:
        targets = self.selected()
        if not targets or self._busy:
            return
        mismatches = [m for m in (branch_mismatch(self.result, p) for p in targets) if m]
        if mismatches and not self.ctx.dialogs.ask("브랜치 확인", "\n".join(mismatches) + "\n\n그래도 이 패키지를 올릴까요?"):
            return
        self._start("check", targets)

    def apply(self) -> None:
        targets = [p for p in self.selected() if self.states[p.name] == "checked"]
        if not targets or self._busy:
            return
        lines = "\n".join(f"  · {p.name} ({p.address}) → {p.server_path or self.project.server_path}" for p in targets)
        prod = [p.name for p in targets if p.is_production]
        warn = f"\n\n⚠ 운영 서버가 있습니다: {', '.join(prod)}. 사용자에게 바로 영향이 갑니다." if prod else ""
        if not self.ctx.dialogs.ask("운영 서버에 반영하기" if prod else "여러 서버에 반영하기",
                                    f"{len(targets)}개 서버에 차례로 백업 후 반영합니다.\n{lines}{warn}\n\n"
                                    "한 서버라도 실패하면 거기서 멈춥니다. 계속할까요?"):
            return
        self._run_hooks = ask_hooks(self.ctx, self.project, self.result, ("pre_apply", "post_apply"))
        self._start("apply", targets)

    def rollback(self) -> None:
        targets = [p for p in self.selected() if self.states[p.name] in ("applied", "apply_failed")]
        if not targets or self._busy:
            return
        names = ", ".join(p.name for p in targets)
        warn = "⚠ 운영 서버가 있습니다.\n\n" if any(p.is_production for p in targets) else ""
        if not self.ctx.dialogs.ask("되돌리기", f"{warn}{names} 서버를 차례로 이번 반영 전으로 되돌릴까요?"):
            return
        self._run_hooks = ask_hooks(self.ctx, self.project, self.result, ("post_rollback",))
        self._start("rollback", targets)

    def cancel(self) -> None:
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.setText("중단 중…")
        if not self.client.cancel():   # 비밀번호 입력 등 연결 전 단계면 끊을 것이 없다
            self.cancel_btn.setEnabled(True)
            self.cancel_btn.setText("취소")

    def _start(self, action: str, targets: List[ServerProfile]) -> None:
        self._action = action
        self._queue = list(targets)
        self._busy = True
        self.cancel_btn.setEnabled(True)
        self.cancel_btn.setText("취소")
        self.state_chip.set("진행 중…", "info")
        self._update_buttons()
        self._next()

    def _next(self) -> None:
        if not self._queue:
            self._finish()
            return
        p = self._queue.pop(0)
        action = self._action
        self.log.banner(f"{p.name}  ({p.address})")
        self._buffer = []
        self._set_state(p.name, RUNNING[action], "")
        package, remote_dir, run_hooks = self.result.out_dir, self.remote_dirs.get(p.name, ""), self._run_hooks

        def make_step(secrets):
            if action == "check":
                return lambda conn, output, progress: remote.upload_and_check(conn, p, secrets, package, output, progress)
            if action == "apply":
                return lambda conn, output, progress: remote.backup_and_apply(conn, p, secrets, remote_dir, output, run_hooks)
            return lambda conn, output, progress: remote.run_action(
                conn, p, secrets, remote_dir, "rollback", output, yes=run_hooks, no_hooks=not run_hooks)

        def done(res: remote.StepResult) -> None:
            self.log.close_stage()
            warning = record_deployment(self.result, self.project, p, res)
            if warning:
                self.log.append(warning)
            if action == "check":
                self.remote_dirs[p.name] = res.remote_dir
                if res.ok:
                    check = remote.parse_check("".join(self._buffer))
                    self.checks[p.name] = check
                    n = check.changes_server if check else None
                    note = "서버와 모두 같아 바뀌는 파일 없음" if n == 0 else f"apply 때 바뀌는 파일 {n}개" if n else "확인 끝"
                    self.log.append("\n✓ 확인 끝.\n")
                    self._set_state(p.name, "checked", note)
                else:
                    self.log.append(f"\n✗ {res.action} 실패 (종료 코드 {res.code})\n")
                    self._set_state(p.name, "check_failed", f"{res.action} 실패 (종료 코드 {res.code})")
                self._next()                                   # 확인은 실패해도 다음 서버로
                return
            when = f"{datetime.now():%H:%M}"
            if action == "apply":
                if res.ok:
                    self.log.append("\n✓ 반영했습니다.\n")
                    self._set_state(p.name, "applied", f"반영 완료 {when}")
                else:
                    self.log.append(f"\n✗ {res.action} 실패 (종료 코드 {res.code})\n")
                    self._set_state(p.name, "apply_failed" if res.action == "apply" else "checked",
                                    f"{res.action} 실패 (종료 코드 {res.code})")
            else:
                self.log.append("\n✓ 되돌렸습니다.\n" if res.ok else f"\n✗ rollback 실패 (종료 코드 {res.code})\n")
                self._set_state(p.name, "rolled_back" if res.ok else "apply_failed",
                                f"되돌림 {when}" if res.ok else f"rollback 실패 (종료 코드 {res.code})")
            files = len(self.result.manifest) + len(self.result.deletes) if action == "apply" else None
            notify_messenger(self.ctx, p, self.project.name, self.result.out_dir.name, res, self.log.append, files=files)
            if res.ok:
                self._next()
            else:
                self._abort("skipped")                          # 반영·되돌리기는 한 서버가 실패하면 멈춘다

        def failed(message: str) -> None:
            self.log.close_stage()
            self.log.append(f"\n[오류] {message}\n")
            if action == "check":
                self._set_state(p.name, "check_failed", message.splitlines()[0] if message else "실행 못 함")
                self._next()
                return
            self._set_state(p.name, "checked" if action == "apply" else "applied", "실행 못 함: " + message.splitlines()[0])
            self._abort("skipped")

        def cancelled() -> None:
            self.log.close_stage()
            self._set_state(p.name, "cancelled" if action == "check" else "checked" if action == "apply" else "applied",
                            "취소함")
            self._abort("skipped")

        self.client.start(p, make_step, self._output, lambda _a, _b: None, done, failed, cancelled)

    def _output(self, text: str) -> None:
        self._buffer.append(text)
        self.log.append(text)

    def _abort(self, state: str) -> None:
        for p in self._queue:
            self._set_state(p.name, state, "앞 서버에서 멈춤")
        self._queue = []
        self._finish()

    def _finish(self) -> None:
        self._busy = False
        picked = [s.name for s in self.selected()]
        counts: Dict[str, int] = {}
        for n in picked:
            counts[self.states[n]] = counts.get(self.states[n], 0) + 1
        parts = [f"{STATE_TEXT[s][0]} {c}" for s, c in counts.items()]
        failed = any(self.states[n] in ("check_failed", "apply_failed", "skipped", "cancelled") for n in picked)
        self.state_chip.set("  ·  ".join(parts) or "끝", "danger" if failed else "success")
        self._update_buttons()
        if self.on_finished:
            self.on_finished()
        window = self.window()
        if window is not None and not window.isActiveWindow():
            self.ctx.notify(f"{self.project.name} 여러 서버 {self._action_text()} " + ("끝 (실패 있음)" if failed else "완료"),
                            "  ·  ".join(parts))

    def _action_text(self) -> str:
        return {"check": "확인", "apply": "반영", "rollback": "되돌리기"}.get(self._action, "")

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._busy:
            self.ctx.dialogs.toast("진행 중에는 닫을 수 없습니다. 먼저 [취소]를 누르세요.", "warning")
            event.ignore()
            return
        super().closeEvent(event)

    def summary(self) -> Dict[str, Any]:
        """테스트·기록용: 서버별 상태와 결과."""
        return {name: (state, self.notes.get(name, "")) for name, state in self.states.items()}
