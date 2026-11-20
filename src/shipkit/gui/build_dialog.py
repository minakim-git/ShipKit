"""빌드 창: 배포 화면의 [빌드] 를 누르면 프로젝트의 빌드 명령을 돌리고 출력을 보여 준다 (취소 가능)."""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QDialog, QHBoxLayout, QVBoxLayout, QWidget

from shipkit import build
from shipkit.config import ProjectConfig
from shipkit.gui.context import AppContext
from shipkit.gui.remote_panel import ServerLog
from shipkit.gui.widgets import Chip, button, label


class BuildDialog(QDialog):
    finished_build = Signal(object)   # build.BuildResult (취소·실패 포함). 배포 화면이 점검을 다시 돌린다

    def __init__(self, ctx: AppContext, project: ProjectConfig, parent: Optional[QWidget] = None, command: str = ""):
        super().__init__(parent)
        self.ctx = ctx
        self.project = project
        self.command = command or build.command_for(project)
        self.result: Optional[build.BuildResult] = None
        self.running = False
        self._cancel = False
        self.setWindowTitle(f"빌드 - {project.name}")
        self.resize(880, 560)
        self.setWindowModality(Qt.WindowModality.NonModal)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(10)
        head = QHBoxLayout()
        head.setSpacing(8)
        self.title = label(self.command, "sectionTitle")
        self.title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.title.setToolTip(f"폴더: {project.root}\n프로젝트 설정의 [빌드 명령]에서 바꿀 수 있습니다.")
        head.addWidget(self.title, 1)
        self.state_chip = Chip("준비", "neutral")
        head.addWidget(self.state_chip)
        layout.addLayout(head)
        self.log = ServerLog()
        layout.addWidget(self.log, 1)
        foot = QHBoxLayout()
        foot.setSpacing(8)
        self.elapsed_label = label("", "muted")
        foot.addWidget(self.elapsed_label)
        foot.addStretch(1)
        self.cancel_btn = button("취소", "danger")
        self.cancel_btn.setToolTip("빌드 프로세스를 끝냅니다 (자식 프로세스까지)")
        self.cancel_btn.clicked.connect(self.cancel)
        self.cancel_btn.hide()
        self.close_btn = button("닫기")
        self.close_btn.clicked.connect(self.close)
        foot.addWidget(self.cancel_btn)
        foot.addWidget(self.close_btn)
        layout.addLayout(foot)
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)
        self._seconds = 0

    # ------------------------------------------------------------------ 실행
    def start(self) -> None:
        if self.running:
            return
        self.running, self._cancel, self.result = True, False, None
        self.log.reset()
        self.state_chip.set("빌드 중…", "info")
        self.cancel_btn.show()
        self.cancel_btn.setEnabled(True)
        self.cancel_btn.setText("취소")
        self.close_btn.setEnabled(False)
        self._seconds = 0
        self.elapsed_label.setText("0초")
        self._timer.start()
        project, command = self.project, self.command

        def done(result: build.BuildResult) -> None:
            self._finish(result)

        def failed(message: str) -> None:
            self.log.append(f"\n[오류] {message}\n")
            self._finish(build.BuildResult(command, 1, float(self._seconds)))
            self.ctx.dialogs.error("빌드", message)

        self.ctx.runner.run_stream(lambda emit: build.run_build(project, emit, lambda: self._cancel, command),
                                   done, failed, self.log.append)

    def _tick(self) -> None:
        self._seconds += 1
        self.elapsed_label.setText(f"{self._seconds}초")

    def _finish(self, result: build.BuildResult) -> None:
        self._timer.stop()
        self.running = False
        self.result = result
        self.log.close_stage()
        self.log.append(("✓ " if result.ok else "[경고] " if result.cancelled else "✗ ") + result.summary() + "\n")
        self.state_chip.set(result.summary(), "success" if result.ok else "warning" if result.cancelled else "danger")
        self.elapsed_label.setText(f"{result.elapsed:.0f}초")
        self.cancel_btn.hide()
        self.close_btn.setEnabled(True)
        if not result.cancelled:
            self.ctx.notify(f"{self.project.name} {result.summary()}", self.command)
        self.finished_build.emit(result)

    def cancel(self) -> None:
        if not self.running:
            return
        self._cancel = True
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.setText("멈추는 중…")

    def closeEvent(self, event) -> None:  # noqa: N802
        if self.running:
            self.ctx.dialogs.toast("빌드가 끝날 때까지 기다리거나 [취소]를 누르세요.", "info")
            event.ignore()
            return
        super().closeEvent(event)
