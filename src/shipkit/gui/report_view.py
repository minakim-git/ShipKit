"""보고서 보기 창: 다른 프로그램 없이 ShipKit 안에서 결과가 먼저 보이게 보여 준다.

기록 파일에 보고서 데이터가 있으면 전용 화면(report.render_html), 없는 예전 패키지는 report.md 를 그린다.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import (
    QBrush, QColor, QGuiApplication, QTextBlockFormat, QTextCursor, QTextFrameFormat, QTextTable,
)
from PySide6.QtWidgets import QDialog, QHBoxLayout, QTextBrowser, QVBoxLayout, QWidget

from shipkit import history, report
from shipkit.gui import theme
from shipkit.gui.widgets import button, label, open_path

_MONO = ("Cascadia Mono", "Consolas", "Malgun Gothic")


class ReportDialog(QDialog):
    """보고서를 읽기 좋게 보여 주는 창. 여러 개를 띄워 놓고 비교할 수 있게 모달이 아니다."""

    def __init__(self, parent: Optional[QWidget], path: Path):
        super().__init__(parent)
        self.path = path
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowTitle(f"보고서 - {path.parent.name}")
        self.resize(940, 780)
        self.setMinimumSize(640, 480)
        try:
            self.markdown = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            self.markdown = f"# 보고서를 읽지 못했습니다\n\n{path}\n\n{e}"

        self.browser = QTextBrowser()
        self.browser.setObjectName("report")
        self.browser.setOpenExternalLinks(True)  # MR·PR 링크는 브라우저로
        data = _report_data(path)
        if data is not None:  # 기록에 정리된 보고서 데이터가 있으면 전용 화면으로
            self.browser.document().setDocumentMargin(26)
            self.browser.setHtml(report.render_html(data, theme.report_colors()))
            theme.on_change(lambda: self.browser.setHtml(report.render_html(data, theme.report_colors())))
        else:                 # 예전 패키지: 마크다운을 여백을 넉넉히 해서 보여 준다
            self.browser.setMarkdown(self.markdown)
            _decorate(self.browser)

        bar = QHBoxLayout()
        self.path_label = label(str(path), "muted", wrap=False)
        self.path_label.setToolTip(str(path))
        self.copy_btn = button("내용 복사", "ghost")
        self.copy_btn.clicked.connect(self.copy)
        open_file = button("다른 프로그램으로 열기", "ghost")
        open_file.clicked.connect(lambda: open_path(self.path))
        open_dir = button("폴더 열기", "ghost")
        open_dir.clicked.connect(lambda: open_path(self.path.parent))
        close = button("닫기", "primary")
        close.clicked.connect(self.close)
        bar.addWidget(self.path_label, 1)
        for b in (self.copy_btn, open_file, open_dir, close):
            bar.addWidget(b)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 14)
        layout.setSpacing(12)
        layout.addWidget(self.browser, 1)
        layout.addLayout(bar)

    def copy(self) -> None:
        QGuiApplication.clipboard().setText(self.markdown)
        self.copy_btn.setText("복사됨")
        QTimer.singleShot(1500, lambda: self.copy_btn.setText("내용 복사"))


def _decorate(browser: QTextBrowser) -> None:
    """마크다운에서 만든 문서에 표 테두리·칸 여백, 코드 배경을 입힌다 (Qt 기본 모양은 테두리가 없다)."""
    doc = browser.document()
    doc.setDocumentMargin(18)
    border = QBrush(QColor(theme.BORDER))
    for frame in doc.rootFrame().childFrames():
        if isinstance(frame, QTextTable):
            fmt = frame.format()
            fmt.setBorder(1)
            fmt.setBorderBrush(border)
            fmt.setBorderStyle(QTextFrameFormat.BorderStyle.BorderStyle_Solid)
            fmt.setBorderCollapse(True)
            fmt.setCellPadding(6)
            fmt.setCellSpacing(0)
            frame.setFormat(fmt)
            for col in range(frame.columns()):  # 머리글 줄
                cell = frame.cellAt(0, col)
                cf = cell.format()
                cf.setBackground(QColor(theme.HEAD))
                cell.setFormat(cf)
    code_bg = QColor(theme.HOVER)
    block = doc.begin()
    while block.isValid():
        fmt = block.blockFormat()
        if fmt.nonBreakableLines():  # ``` 코드 블록
            fmt.setBackground(code_bg)
            fmt.setLeftMargin(6)
        elif fmt.headingLevel():     # 제목 위는 넉넉히 띄워 단락을 구분한다
            fmt.setTopMargin(26 if fmt.headingLevel() > 1 else 4)
            fmt.setBottomMargin(10)
        elif block.textList() is not None:
            fmt.setBottomMargin(5)
        else:
            fmt.setBottomMargin(8)
        fmt.setLineHeight(135, QTextBlockFormat.LineHeightTypes.ProportionalHeight.value)
        QTextCursor(block).setBlockFormat(fmt)
        block = block.next()
    # 인라인 코드(`...`)는 고정폭 글꼴 표시만 남아 있으므로 배경을 깔아 구분한다
    cursor = QTextCursor(doc)
    block = doc.begin()
    while block.isValid():
        it = block.begin()
        while not it.atEnd():
            frag = it.fragment()
            if frag.isValid() and frag.charFormat().fontFixedPitch():
                cursor.setPosition(frag.position())
                cursor.setPosition(frag.position() + frag.length(), QTextCursor.MoveMode.KeepAnchor)
                cf = frag.charFormat()
                cf.setBackground(code_bg)
                cf.setFontFamilies(list(_MONO))
                cursor.setCharFormat(cf)
            it += 1
        block = block.next()


def _report_data(path: Path) -> Optional[Dict[str, Any]]:
    """보고서 옆 기록 파일에 담아 둔 보고서 데이터 (없거나 예전 패키지면 None)."""
    record = path.parent / history.RECORD_FILE
    try:
        data = json.loads(record.read_text(encoding="utf-8")).get("report")
    except (OSError, ValueError, AttributeError):
        return None
    return data if isinstance(data, dict) else None


def show_report(parent: Optional[QWidget], path: Path) -> ReportDialog:
    dialog = ReportDialog(parent, path)
    dialog.show()
    return dialog
