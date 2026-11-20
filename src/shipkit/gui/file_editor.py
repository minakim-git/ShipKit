"""서버 파일 편집기: 서버 화면에서 텍스트 파일 하나를 열어 고치고 저장한다 (설정 파일 한 줄 고치려고 vi 를 켜지 않게).

읽기·쓰기(SFTP)는 서버 화면이 한 번에 하나씩 돌리고, 이 창은 글과 상태만 다룬다. 인코딩(UTF-8·CP949…)과 줄 끝(LF·CRLF)은
읽을 때 알아내 저장할 때 그대로 되돌린다. 저장 전에 서버의 원본을 같은 폴더에 '<이름>.bak' 으로 남긴다 (끌 수 있다).
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QSettings, Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import QCheckBox, QDialog, QHBoxLayout, QVBoxLayout, QWidget

from shipkit import remote
from shipkit.gui.code_editor import KIND_NAMES, CodeEditor, FindBar, Highlighter, kind_of
from shipkit.gui.context import AppContext
from shipkit.gui.widgets import Chip, button, label

BACKUP_KEY = "server/edit_backup"   # 저장 전 .bak 백업 (QSettings)


class FileEditorDialog(QDialog):
    save_requested = Signal(bytes, bool)   # (저장할 내용, 백업할지) → 서버 화면이 SFTP 로 쓴다
    reload_requested = Signal()            # [다시 읽기]

    def __init__(self, ctx: AppContext, path: str, data: bytes, encodings, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.ctx = ctx
        self.path = path
        self.original = data
        self.text, self.encoding, self.newline = remote.decode_text(data, encodings)
        self.saving = False
        self.setWindowTitle(f"{path.rsplit('/', 1)[-1]} - 서버 파일 편집")
        self.resize(900, 640)
        self.setWindowModality(Qt.WindowModality.NonModal)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(10)
        head = QHBoxLayout()
        head.setSpacing(8)
        self.path_label = label(path, "sectionTitle")
        self.path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        head.addWidget(self.path_label, 1)
        self.format_chip = Chip(self._format_text(), "neutral")
        self.format_chip.setToolTip("읽을 때 알아낸 인코딩과 줄 끝. 저장할 때 그대로 씁니다.")
        self.state_chip = Chip("", "neutral")
        self.kind = kind_of(path)
        if self.kind:
            self.kind_chip = Chip(KIND_NAMES[self.kind], "neutral")
            self.kind_chip.setToolTip("파일 종류에 맞춰 주석·키·값을 색으로 구분합니다 (보기 편하게 할 뿐 검사는 아님)")
            head.addWidget(self.kind_chip)
        head.addWidget(self.format_chip)
        head.addWidget(self.state_chip)
        layout.addLayout(head)

        self.editor = CodeEditor()
        self.editor.setTabStopDistance(4 * self.editor.fontMetrics().horizontalAdvance(" "))
        self.editor.setPlainText(self.text)
        self.highlighter = Highlighter(self.editor.document(), self.kind) if self.kind else None
        self.editor.textChanged.connect(self._changed)
        self.editor.cursorPositionChanged.connect(self._update_position)
        self.find_bar = FindBar(self.editor, self)
        layout.addWidget(self.find_bar)
        layout.addWidget(self.editor, 1)

        foot = QHBoxLayout()
        foot.setSpacing(8)
        self.backup_check = QCheckBox("저장 전에 서버의 원본을 같은 폴더에 .bak 으로 남김")
        self.backup_check.setChecked(str(QSettings().value(BACKUP_KEY, "true")).lower() == "true")
        self.backup_check.toggled.connect(lambda on: QSettings().setValue(BACKUP_KEY, "true" if on else "false"))
        foot.addWidget(self.backup_check)
        foot.addStretch(1)
        self.position_label = label("", "muted")
        self.position_label.setToolTip("커서 위치 (줄, 칸)")
        foot.addWidget(self.position_label)
        self.find_btn = button("찾기", "ghost")
        self.find_btn.setToolTip("Ctrl+F (바꾸기는 Ctrl+H)")
        self.find_btn.clicked.connect(lambda: self.find_bar.open())
        foot.addWidget(self.find_btn)
        self.reload_btn = button("다시 읽기", "ghost")
        self.reload_btn.setToolTip("서버의 지금 내용을 다시 읽습니다 (고친 것은 버림)")
        self.reload_btn.clicked.connect(self.reload)
        self.save_btn = button("저장", "primary")
        self.save_btn.setToolTip("Ctrl+S")
        self.save_btn.clicked.connect(self.save)
        self.close_btn = button("닫기")
        self.close_btn.clicked.connect(self.close)
        foot.addWidget(self.reload_btn)
        foot.addWidget(self.save_btn)
        foot.addWidget(self.close_btn)
        layout.addLayout(foot)
        QShortcut(QKeySequence("Ctrl+S"), self, activated=self.save)
        QShortcut(QKeySequence("Ctrl+F"), self, activated=lambda: self.find_bar.open())
        QShortcut(QKeySequence("Ctrl+H"), self, activated=lambda: self.find_bar.open(replace=True))
        QShortcut(QKeySequence("F3"), self, activated=self._find_next)
        QShortcut(QKeySequence("Shift+F3"), self, activated=self._find_prev)
        self._set_state()
        self._update_position()
        self.editor.setFocus()

    def changeEvent(self, event) -> None:  # noqa: N802
        super().changeEvent(event)
        if event.type() in (event.Type.StyleChange, event.Type.PaletteChange):
            self._theme_changed()        # 테마가 바뀌면 강조 색도

    def _find_next(self) -> None:
        if self.find_bar.text:
            self.find_bar.next()
        else:
            self.find_bar.open()

    def _find_prev(self) -> None:
        if self.find_bar.text:
            self.find_bar.prev()
        else:
            self.find_bar.open()

    def _theme_changed(self) -> None:
        if self.highlighter is not None:
            self.highlighter.refresh()
        self.editor.viewport().update()

    def _update_position(self) -> None:
        cur = self.editor.textCursor()
        self.position_label.setText(f"줄 {cur.blockNumber() + 1}, 칸 {cur.positionInBlock() + 1}")

    # ------------------------------------------------------------------ 상태
    def _format_text(self) -> str:
        enc = {"utf-8": "UTF-8", "utf-8-sig": "UTF-8 (BOM)", "cp949": "CP949", "euc-kr": "EUC-KR"}.get(self.encoding, self.encoding)
        return f"{enc} · {'CRLF' if self.newline == chr(13) + chr(10) else 'LF'}"

    @property
    def dirty(self) -> bool:
        return self.editor.toPlainText() != self.text

    def _changed(self) -> None:
        self._set_state()

    def _set_state(self) -> None:
        if self.saving:
            self.state_chip.set("저장 중…", "info")
        elif self.dirty:
            self.state_chip.set("수정됨", "warning")
        else:
            self.state_chip.set("저장됨", "success")
        self.save_btn.setEnabled(self.dirty and not self.saving)
        self.reload_btn.setEnabled(not self.saving)
        self.editor.setReadOnly(self.saving)
        self.find_bar._changed()         # 저장 중에는 바꾸기 막기

    def content(self) -> bytes:
        """지금 글을 읽을 때의 인코딩·줄 끝으로 되돌린 바이트."""
        return remote.encode_text(self.editor.toPlainText(), self.encoding, self.newline)

    # ------------------------------------------------------------------ 동작
    def save(self) -> None:
        if not self.dirty or self.saving:
            return
        try:
            data = self.content()
        except UnicodeEncodeError as e:
            self.ctx.dialogs.error("저장", f"{self._format_text().split(' ·')[0]} 로 적을 수 없는 글자가 있습니다: {e}")
            return
        self.saving = True
        self._set_state()
        self.save_requested.emit(data, self.backup_check.isChecked())

    def saved(self, data: bytes, backup_path: str = "") -> None:
        """서버 화면이 저장을 마쳤을 때."""
        self.saving = False
        self.original = data
        self.text = self.editor.toPlainText()
        self._set_state()
        self.ctx.dialogs.toast(f"{self.path} 에 저장했습니다." + (f" 원본은 {backup_path}" if backup_path else ""))

    def save_failed(self, message: str) -> None:
        self.saving = False
        self._set_state()
        self.ctx.dialogs.error("저장하지 못했습니다", message + "\n\n접속 계정이 쓸 수 없는 파일이면 터미널에서 sudo 로 고치세요.")

    def reload(self) -> None:
        if self.dirty and not self.ctx.dialogs.ask("다시 읽기", "고친 내용을 버리고 서버의 지금 내용을 다시 읽을까요?"):
            return
        self.reload_requested.emit()

    def reloaded(self, data: bytes, encodings) -> None:
        self.original = data
        self.text, self.encoding, self.newline = remote.decode_text(data, encodings)
        self.editor.setPlainText(self.text)
        self.format_chip.set(self._format_text(), "neutral")
        self._set_state()

    def closeEvent(self, event) -> None:  # noqa: N802
        if self.saving:
            event.ignore()
            return
        if self.dirty and not self.ctx.dialogs.ask("닫기", "저장하지 않은 변경이 있습니다. 버리고 닫을까요?"):
            event.ignore()
            return
        super().closeEvent(event)
