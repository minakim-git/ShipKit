"""파일 차이 보기: 서버에 있는 파일과 패키지의 파일을 줄 단위(unified diff)로 비교해 보여 준다.

완료 화면의 올리고 확인(check) 결과 표에서 '변경' 파일을 고르면 열린다. 왼쪽(-)이 지금 서버, 오른쪽(+)이 패키지라서
반영하면 무엇이 어떻게 바뀌는지 그대로 읽힌다. 줄 끝(CRLF/LF)·인코딩 차이는 글 비교에서 빼고 위에 따로 알린다.
"""
from __future__ import annotations

import difflib
from typing import List, Optional, Tuple

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QSyntaxHighlighter, QTextCharFormat, QTextDocument
from PySide6.QtWidgets import QDialog, QHBoxLayout, QPlainTextEdit, QVBoxLayout, QWidget

from shipkit.gui import theme
from shipkit.gui.widgets import Chip, button, label

CONTEXT = 3   # 바뀐 줄 앞뒤로 보여 줄 줄 수


def unified_diff(old: str, new: str, old_label: str, new_label: str) -> Tuple[List[str], int, int]:
    """두 글의 unified diff 줄 목록과 (더한 줄, 뺀 줄) 수. 같으면 빈 목록."""
    a, b = old.splitlines(), new.splitlines()
    lines = list(difflib.unified_diff(a, b, old_label, new_label, lineterm="", n=CONTEXT))
    added = sum(1 for ln in lines[2:] if ln.startswith("+"))
    removed = sum(1 for ln in lines[2:] if ln.startswith("-"))
    return lines, added, removed


class DiffHighlighter(QSyntaxHighlighter):
    """+ 줄은 초록, - 줄은 빨강, @@ 줄은 파랑, 머리 두 줄은 굵게."""

    def __init__(self, document: QTextDocument):
        super().__init__(document)
        self.refresh()

    def refresh(self) -> None:
        tones = theme.TONES

        def fmt(fg: str, bg: str = "", bold: bool = False) -> QTextCharFormat:
            f = QTextCharFormat()
            f.setForeground(QColor(fg))
            if bg:
                f.setBackground(QColor(bg))
            if bold:
                f.setFontWeight(QFont.Weight.DemiBold)
            return f

        self.formats = {
            "+": fmt(tones["success"][0], tones["success"][1]),
            "-": fmt(tones["danger"][0], tones["danger"][1]),
            "@": fmt(tones["info"][0], "", True),
            "h": fmt(theme.MUTED, "", True),
        }
        self.rehighlight()

    def highlightBlock(self, text: str) -> None:  # noqa: N802
        if text.startswith("+++ ") or text.startswith("--- "):
            key = "h"
        elif text.startswith("@@"):
            key = "@"
        elif text[:1] in ("+", "-"):
            key = text[0]
        else:
            return
        self.setFormat(0, len(text), self.formats[key])


class DiffDialog(QDialog):
    """서버 파일(-)과 패키지 파일(+)의 차이."""

    def __init__(self, path: str, old: str, new: str, old_label: str = "서버", new_label: str = "패키지",
                 note: str = "", parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.path = path
        self.lines, self.added, self.removed = unified_diff(old, new, f"{old_label}  {path}", f"{new_label}  {path}")
        self.setWindowTitle(f"{path.rsplit('/', 1)[-1]} - 차이 보기")
        self.resize(960, 680)
        self.setWindowModality(Qt.WindowModality.NonModal)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(10)
        head = QHBoxLayout()
        head.setSpacing(8)
        title = label(path, "sectionTitle")
        title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        head.addWidget(title, 1)
        self.stat_chip = Chip(self._stat_text(), "neutral" if not self.lines else "warning")
        self.stat_chip.setToolTip(f"-: 지금 {old_label}에 있는 줄 (반영하면 없어짐)   +: {new_label}의 줄 (반영하면 생김)")
        head.addWidget(self.stat_chip)
        layout.addLayout(head)
        self.note_label = label(note, "muted")
        self.note_label.setVisible(bool(note))
        layout.addWidget(self.note_label)

        self.view = QPlainTextEdit()
        self.view.setObjectName("codeEditor")
        self.view.setReadOnly(True)
        self.view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.view.setPlainText("\n".join(self.lines) if self.lines else
                               f"{old_label}와 {new_label}의 내용이 같습니다." + ("" if not note else " (아래 알림만 다릅니다)"))
        self.highlighter = DiffHighlighter(self.view.document())
        layout.addWidget(self.view, 1)

        foot = QHBoxLayout()
        foot.addWidget(label(f"- {old_label} (지금 서버)   + {new_label} (반영 후)", "muted"))
        foot.addStretch(1)
        self.close_btn = button("닫기")
        self.close_btn.clicked.connect(self.close)
        foot.addWidget(self.close_btn)
        layout.addLayout(foot)

    def _stat_text(self) -> str:
        if not self.lines:
            return "같음"
        return f"+{self.added} −{self.removed}"

    def changeEvent(self, event) -> None:  # noqa: N802
        super().changeEvent(event)
        if event.type() in (event.Type.StyleChange, event.Type.PaletteChange):
            self.highlighter.refresh()
