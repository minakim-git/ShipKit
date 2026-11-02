"""사용자 가이드 창: 화면에는 설명글을 두지 않는 대신, 패키지에 넣어 둔 docs/user-guide.md 를 앱 안에서 보여 준다."""
from __future__ import annotations

import re
from importlib import resources
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QDialog, QHBoxLayout, QTextBrowser, QVBoxLayout, QWidget

from shipkit.gui import theme
from shipkit.gui.report_view import _decorate
from shipkit.gui.widgets import button, label

_open: Optional["GuideDialog"] = None


def guide_markdown() -> str:
    try:
        return (resources.files("shipkit") / "docs" / "user-guide.md").read_text(encoding="utf-8")
    except (OSError, TypeError) as e:
        return f"# 사용자 가이드\n\n가이드 파일을 찾지 못했습니다 ({e})."


def _slug(heading: str) -> str:
    """GitHub 식 제목 앵커: 소문자, 문장 부호 제거, 공백은 - (가이드의 목차 링크와 맞춘다)."""
    return re.sub(r"[^\w\s-]", "", heading.strip().lower()).replace(" ", "-")


def _anchor_headings(browser: QTextBrowser) -> None:
    """마크다운 제목마다 앵커 이름을 붙여, 목차의 #링크로 그 자리로 갈 수 있게 한다."""
    doc = browser.document()
    block = doc.begin()
    while block.isValid():
        if block.blockFormat().headingLevel() and block.text():
            cursor = QTextCursor(block)
            cursor.select(QTextCursor.SelectionType.BlockUnderCursor)
            fmt = cursor.charFormat()
            fmt.setAnchor(True)
            fmt.setAnchorNames([_slug(block.text())])
            cursor.mergeCharFormat(fmt)
        block = block.next()


class GuideDialog(QDialog):
    """가이드를 읽는 창. 작업하면서 옆에 두고 볼 수 있게 모달이 아니다."""

    def __init__(self, parent: Optional[QWidget]):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowTitle("사용자 가이드")
        self.resize(900, 760)
        self.setMinimumSize(560, 420)
        self.markdown = guide_markdown()

        self.browser = QTextBrowser()
        self.browser.setObjectName("report")
        self.browser.setOpenExternalLinks(True)  # #목차 링크는 창 안에서 이동
        self._render()
        theme.on_change(self._render)

        bar = QHBoxLayout()
        bar.addWidget(label("", "muted"), 1)
        close = button("닫기", "primary")
        close.clicked.connect(self.close)
        bar.addWidget(close)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 14)
        layout.setSpacing(12)
        layout.addWidget(self.browser, 1)
        layout.addLayout(bar)

    def _render(self) -> None:
        self.browser.setMarkdown(self.markdown)
        _decorate(self.browser)
        _anchor_headings(self.browser)


def show_guide(parent: Optional[QWidget]) -> GuideDialog:
    """가이드 창을 연다. 이미 열려 있으면 그 창을 앞으로 가져온다."""
    global _open
    if _open is not None:
        try:
            _open.raise_()
            _open.activateWindow()
            return _open
        except RuntimeError:  # 이미 닫혀 지워진 창
            _open = None
    dialog = GuideDialog(parent)
    dialog.destroyed.connect(lambda *_: globals().__setitem__("_open", None))
    _open = dialog
    dialog.show()
    return dialog
