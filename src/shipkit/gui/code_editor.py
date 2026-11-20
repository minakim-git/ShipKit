"""코드 편집 칸: 줄 번호·현재 줄 강조·찾기/바꾸기와, 파일 종류별 간단한 구문 강조.

서버 파일 편집기(file_editor)가 쓴다. 강조는 설정 파일을 읽기 쉽게 하는 정도(주석·키·값·문자열·태그)이고 파서가 아니다.
색은 테마 색(theme)에서 가져와 밝게·어둡게 모두 맞는다.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from PySide6.QtCore import QRect, QRegularExpression, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor, QFont, QKeySequence, QPainter, QShortcut, QSyntaxHighlighter, QTextCharFormat, QTextCursor, QTextDocument,
    QTextFormat,
)
from PySide6.QtWidgets import QCheckBox, QGridLayout, QLineEdit, QPlainTextEdit, QTextEdit, QWidget

from shipkit.gui import theme
from shipkit.gui.widgets import button, label

# ---------------------------------------------------------------------- 파일 종류
# 확장자(또는 이름) → 강조 종류. 모르는 파일은 강조 없음.
KINDS: Dict[str, str] = {
    "properties": "properties", "conf": "properties", "cfg": "properties", "ini": "properties", "env": "properties",
    "toml": "properties", "cnf": "properties",
    "xml": "xml", "html": "xml", "htm": "xml", "jsp": "xml", "jspx": "xml", "xsl": "xml", "xslt": "xml", "tld": "xml",
    "wsdl": "xml", "xsd": "xml", "svg": "xml", "vue": "xml", "tag": "xml",
    "sh": "shell", "bash": "shell", "ksh": "shell", "zsh": "shell", "bashrc": "shell", "profile": "shell",
    "bash_profile": "shell", "service": "properties",
    "yml": "yaml", "yaml": "yaml",
    "sql": "sql",
    "json": "json",
    "py": "python",
    "js": "js", "ts": "js", "java": "js", "groovy": "js", "kt": "js",
}
KIND_NAMES = {"properties": "설정(key=value)", "xml": "XML/HTML", "shell": "셸 스크립트", "yaml": "YAML", "sql": "SQL",
              "json": "JSON", "python": "Python", "js": "Java/JS"}


def kind_of(name: str) -> str:
    """파일 이름으로 강조 종류를 고른다 ('' 이면 강조 없음)."""
    base = name.rsplit("/", 1)[-1].lower()
    if base in KINDS:
        return KINDS[base]
    if base.startswith(".") and base[1:] in KINDS:
        return KINDS[base[1:]]
    ext = base.rsplit(".", 1)[-1] if "." in base else ""
    return KINDS.get(ext, "")


# ---------------------------------------------------------------------- 구문 강조
_SQL_KW = ("select from where insert into values update set delete create table alter drop index view and or not null "
           "is in like between join left right inner outer on as order by group having limit offset union all distinct "
           "primary key foreign references default constraint begin commit rollback if exists exists case when then "
           "else end truncate grant revoke with")
_SHELL_KW = "if then else elif fi for while until do done case esac in function return exit export local set unset"
_PY_KW = ("def class return if elif else for while in not and or is None True False import from as try except finally "
          "raise with lambda pass break continue yield global nonlocal assert del async await")
_JS_KW = ("function return if else for while do switch case break continue new this var let const class extends import "
          "export default try catch finally throw typeof instanceof null undefined true false public private protected "
          "static final void int long boolean String package interface implements extends throws abstract")


def _words(words: str) -> str:
    return r"\b(?:" + "|".join(re.escape(w) for w in words.split()) + r")\b"


class Highlighter(QSyntaxHighlighter):
    """파일 종류별 규칙으로 칠한다. 규칙은 (정규식, 서식 이름) 목록이고, 여러 줄 주석은 블록 상태로 잇는다."""

    def __init__(self, document: QTextDocument, kind: str):
        super().__init__(document)
        self.kind = kind
        self.rules: List[Tuple[QRegularExpression, str]] = []
        self.block_comment: Optional[Tuple[QRegularExpression, QRegularExpression]] = None
        self.formats: Dict[str, QTextCharFormat] = {}
        self.refresh()

    def refresh(self) -> None:
        """테마 색으로 서식을 다시 만든다 (테마가 바뀌었을 때도)."""
        self.formats = _formats()
        self.rules = [(QRegularExpression(p), f) for p, f in _rules(self.kind)]
        self.block_comment = _block_comment(self.kind)
        self.rehighlight()

    def highlightBlock(self, text: str) -> None:  # noqa: N802
        for rx, name in self.rules:
            it = rx.globalMatch(text)
            fmt = self.formats[name]
            while it.hasNext():
                m = it.next()
                # 그룹이 있으면 그룹만 (key=value 의 key 처럼), 없으면 전체
                idx = 1 if m.lastCapturedIndex() >= 1 else 0
                self.setFormat(m.capturedStart(idx), m.capturedLength(idx), fmt)
        self.setCurrentBlockState(0)
        if self.block_comment is None:
            return
        start_rx, end_rx = self.block_comment
        start = 0
        if self.previousBlockState() != 1:
            m = start_rx.match(text)
            start = m.capturedStart() if m.hasMatch() else -1
        while start >= 0:
            m = end_rx.match(text, start)
            if not m.hasMatch():
                self.setCurrentBlockState(1)
                length = len(text) - start
            else:
                length = m.capturedEnd() - start
            self.setFormat(start, length, self.formats["comment"])
            m2 = start_rx.match(text, start + length)
            start = m2.capturedStart() if m2.hasMatch() else -1


def _fmt(color: str, bold: bool = False, italic: bool = False) -> QTextCharFormat:
    f = QTextCharFormat()
    f.setForeground(QColor(color))
    if bold:
        f.setFontWeight(QFont.Weight.DemiBold)
    if italic:
        f.setFontItalic(True)
    return f


def _formats() -> Dict[str, QTextCharFormat]:
    tones = theme.TONES
    return {
        "comment": _fmt(theme.MUTED, italic=True),
        "key": _fmt(theme.PRIMARY),
        "tag": _fmt(theme.PRIMARY, bold=True),
        "attr": _fmt(tones["info"][0]),
        "string": _fmt(tones["success"][0]),
        "keyword": _fmt(tones["info"][0], bold=True),
        "number": _fmt(tones["warning"][0]),
        "var": _fmt(tones["warning"][0]),
    }


_STRING = r"\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'"


def _rules(kind: str) -> List[Tuple[str, str]]:
    if kind == "properties":
        return [(r"^\s*([^#;!=:\s][^=:]*?)\s*[=:]", "key"), (_STRING, "string"), (r"\$\{[^}]*\}", "var"),
                (r"^\s*\[[^\]]*\]\s*$", "tag"), (r"^\s*[#;!].*$", "comment")]
    if kind == "xml":
        return [(r"</?\s*([A-Za-z_][\w:.-]*)", "tag"), (r"[/?]?>", "tag"), (r"\b([A-Za-z_][\w:.-]*)\s*=", "attr"),
                (_STRING, "string"), (r"\$\{[^}]*\}", "var"), (r"<%[\s\S]*?%>", "keyword")]
    if kind == "shell":
        return [(_words(_SHELL_KW), "keyword"), (r"\$\{[^}]*\}|\$\w+|\$[@#?*$!0-9]", "var"), (_STRING, "string"),
                (r"(?<![\w$])#.*$", "comment")]
    if kind == "yaml":
        return [(r"^\s*-?\s*([^#\s][^:#]*?)\s*:(?:\s|$)", "key"), (_STRING, "string"), (r"\b-?\d+(?:\.\d+)?\b", "number"),
                (r"\b(?:true|false|null|yes|no|on|off)\b", "keyword"), (r"(?<![\w])#.*$", "comment")]
    if kind == "sql":
        return [(r"(?i)" + _words(_SQL_KW), "keyword"), (r"'(?:[^']|'')*'", "string"), (r"\b\d+(?:\.\d+)?\b", "number"),
                (r"--.*$", "comment")]
    if kind == "json":
        return [(r"\"(?:[^\"\\]|\\.)*\"\s*(?=:)", "key"), (r"\"(?:[^\"\\]|\\.)*\"", "string"),
                (r"\b-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b", "number"), (r"\b(?:true|false|null)\b", "keyword")]
    if kind == "python":
        return [(_words(_PY_KW), "keyword"), (r"\b\d+(?:\.\d+)?\b", "number"), (_STRING, "string"), (r"#.*$", "comment")]
    if kind == "js":
        return [(_words(_JS_KW), "keyword"), (r"\b\d+(?:\.\d+)?[LlFfDd]?\b", "number"), (_STRING, "string"),
                (r"//.*$", "comment")]
    return []


def _block_comment(kind: str) -> Optional[Tuple[QRegularExpression, QRegularExpression]]:
    if kind == "xml":
        return QRegularExpression(r"<!--"), QRegularExpression(r"-->")
    if kind in ("sql", "js"):
        return QRegularExpression(r"/\*"), QRegularExpression(r"\*/")
    return None


# ---------------------------------------------------------------------- 편집 칸
class _LineNumbers(QWidget):
    def __init__(self, editor: "CodeEditor"):
        super().__init__(editor)
        self.editor = editor

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(self.editor.gutter_width(), 0)

    def paintEvent(self, event) -> None:  # noqa: N802
        self.editor.paint_gutter(event)


class CodeEditor(QPlainTextEdit):
    """줄 번호와 현재 줄 강조가 있는 편집 칸. 찾기/바꾸기는 find_text / replace_one / replace_all."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("codeEditor")
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.gutter = _LineNumbers(self)
        self._match_selections: List[QTextEdit.ExtraSelection] = []
        self.blockCountChanged.connect(self._update_gutter_width)
        self.updateRequest.connect(self._update_gutter)
        self.cursorPositionChanged.connect(self._highlight_line)
        self._update_gutter_width()
        self._highlight_line()

    # ---- 줄 번호
    def gutter_width(self) -> int:
        digits = max(2, len(str(max(1, self.blockCount()))))
        return 14 + self.fontMetrics().horizontalAdvance("9") * digits

    def _update_gutter_width(self, *_a) -> None:
        self.setViewportMargins(self.gutter_width(), 0, 0, 0)

    def _update_gutter(self, rect: QRect, dy: int) -> None:
        if dy:
            self.gutter.scroll(0, dy)
        else:
            self.gutter.update(0, rect.y(), self.gutter.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self._update_gutter_width()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        cr = self.contentsRect()
        self.gutter.setGeometry(QRect(cr.left(), cr.top(), self.gutter_width(), cr.height()))

    def changeEvent(self, event) -> None:  # noqa: N802
        super().changeEvent(event)
        if event.type() in (event.Type.FontChange, event.Type.StyleChange):
            self._update_gutter_width()

    def paint_gutter(self, event) -> None:
        painter = QPainter(self.gutter)
        painter.fillRect(event.rect(), QColor(theme.HEAD))
        painter.setPen(QColor(theme.BORDER))
        painter.drawLine(event.rect().right(), event.rect().top(), event.rect().right(), event.rect().bottom())
        block = self.firstVisibleBlock()
        top = round(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        bottom = top + round(self.blockBoundingRect(block).height())
        current = self.textCursor().blockNumber()
        width, height = self.gutter.width() - 6, self.fontMetrics().height()
        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                n = block.blockNumber()
                painter.setPen(QColor(theme.TEXT if n == current else theme.SUBTLE))
                painter.drawText(0, top, width, height, Qt.AlignmentFlag.AlignRight, str(n + 1))
            block = block.next()
            top = bottom
            bottom = top + round(self.blockBoundingRect(block).height())
        painter.end()

    def _highlight_line(self) -> None:
        sel = QTextEdit.ExtraSelection()
        sel.format.setBackground(QColor(theme.HOVER))
        sel.format.setProperty(QTextFormat.Property.FullWidthSelection, True)
        sel.cursor = self.textCursor()
        sel.cursor.clearSelection()
        self.setExtraSelections([sel] + self._match_selections)
        self.gutter.update()

    MATCH_LIMIT = 2000   # 이보다 많으면 강조하지 않음 (큰 파일에서 느려지지 않게)

    def highlight_matches(self, text: str, case: bool = False) -> int:
        """찾는 글이 나오는 곳을 모두 옅게 칠한다 (찾기 막대가 열려 있는 동안). 반환: 칠한 개수."""
        self._match_selections = []
        if text:
            doc, cur = self.document(), QTextCursor(self.document())
            fmt = QTextCharFormat()
            fmt.setBackground(QColor(theme.TONES["warning"][1]))
            fmt.setForeground(QColor(theme.TONES["warning"][0]))
            while len(self._match_selections) < self.MATCH_LIMIT:
                cur = doc.find(text, cur, self._flags(case))
                if cur.isNull():
                    break
                sel = QTextEdit.ExtraSelection()
                sel.cursor, sel.format = QTextCursor(cur), fmt
                self._match_selections.append(sel)
            if len(self._match_selections) >= self.MATCH_LIMIT:
                self._match_selections = []
        self._highlight_line()
        return len(self._match_selections)

    # ---- 찾기/바꾸기
    def _flags(self, case: bool, backward: bool = False) -> QTextDocument.FindFlag:
        flags = QTextDocument.FindFlag(0)
        if case:
            flags |= QTextDocument.FindFlag.FindCaseSensitively
        if backward:
            flags |= QTextDocument.FindFlag.FindBackward
        return flags

    def find_text(self, text: str, case: bool = False, backward: bool = False) -> bool:
        """다음(이전) 것을 골라 보여 준다. 끝에 닿으면 처음(끝)부터 다시. 없으면 False."""
        if not text:
            return False
        if self.find(text, self._flags(case, backward)):
            return True
        cur = self.textCursor()
        cur.movePosition(QTextCursor.MoveOperation.End if backward else QTextCursor.MoveOperation.Start)
        self.setTextCursor(cur)
        return self.find(text, self._flags(case, backward))

    def count_matches(self, text: str, case: bool = False) -> int:
        if not text:
            return 0
        doc = self.document()
        n, cur = 0, QTextCursor(doc)
        flags = self._flags(case)
        while True:
            cur = doc.find(text, cur, flags)
            if cur.isNull():
                return n
            n += 1

    def match_index(self, text: str, case: bool = False) -> int:
        """지금 고른 것이 몇 번째인지 (1부터). 고른 것이 찾는 글이 아니면 0."""
        sel = self.textCursor()
        if not sel.hasSelection() or not text:
            return 0
        picked = sel.selectedText()
        if (picked != text) if case else (picked.lower() != text.lower()):
            return 0
        doc, cur, n = self.document(), QTextCursor(self.document()), 0
        while True:
            cur = doc.find(text, cur, self._flags(case))
            if cur.isNull():
                return 0
            n += 1
            if cur.selectionStart() == sel.selectionStart():
                return n

    def replace_one(self, text: str, new: str, case: bool = False) -> bool:
        """고른 것이 찾는 글이면 바꾸고 다음으로. 아니면 다음 것을 고르기만 한다."""
        if self.match_index(text, case):
            cur = self.textCursor()
            cur.insertText(new)
        return self.find_text(text, case)

    def replace_all(self, text: str, new: str, case: bool = False) -> int:
        if not text:
            return 0
        doc, n = self.document(), 0
        cur = QTextCursor(doc)
        cur.beginEditBlock()
        pos = QTextCursor(doc)
        while True:
            pos = doc.find(text, pos, self._flags(case))
            if pos.isNull():
                break
            pos.insertText(new)
            n += 1
        cur.endEditBlock()
        return n


# ---------------------------------------------------------------------- 찾기/바꾸기 막대
class FindBar(QWidget):
    """편집 칸 위에 접었다 펴는 찾기/바꾸기 줄. Ctrl+F 로 열고 Esc 로 닫으며, Enter 는 다음·Shift+Enter 는 이전."""
    closed = Signal()

    def __init__(self, editor: CodeEditor, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.editor = editor
        self.setObjectName("findBar")
        grid = QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(4)
        self.find_edit = QLineEdit()
        self.find_edit.setPlaceholderText("찾을 글")
        self.find_edit.setClearButtonEnabled(True)
        self.find_edit.textChanged.connect(self._changed)
        self.find_edit.returnPressed.connect(self.next)
        self.replace_edit = QLineEdit()
        self.replace_edit.setPlaceholderText("바꿀 글")
        self.replace_edit.returnPressed.connect(self.replace)
        self.case_check = QCheckBox("대소문자 구분")
        self.case_check.toggled.connect(self._changed)
        self.prev_btn = button("이전", "ghost")
        self.prev_btn.setToolTip("Shift+Enter · Shift+F3")
        self.prev_btn.clicked.connect(self.prev)
        self.next_btn = button("다음", "ghost")
        self.next_btn.setToolTip("Enter · F3")
        self.next_btn.clicked.connect(self.next)
        self.count_label = label("", "muted")
        self.count_label.setMinimumWidth(56)
        self.replace_btn = button("바꾸기", "ghost")
        self.replace_btn.setToolTip("고른 것을 바꾸고 다음으로")
        self.replace_btn.clicked.connect(self.replace)
        self.replace_all_btn = button("모두 바꾸기", "ghost")
        self.replace_all_btn.clicked.connect(self.replace_all)
        self.close_btn = button("닫기", "ghost")
        self.close_btn.setToolTip("Esc")
        self.close_btn.clicked.connect(self.hide_bar)
        grid.addWidget(self.find_edit, 0, 0)
        grid.addWidget(self.case_check, 0, 1)
        grid.addWidget(self.prev_btn, 0, 2)
        grid.addWidget(self.next_btn, 0, 3)
        grid.addWidget(self.count_label, 0, 4)
        grid.addWidget(self.close_btn, 0, 5)
        grid.addWidget(self.replace_edit, 1, 0)
        grid.addWidget(self.replace_btn, 1, 2)
        grid.addWidget(self.replace_all_btn, 1, 3, 1, 2)
        grid.setColumnStretch(0, 1)
        for w in (self.find_edit, self.replace_edit):
            QShortcut(QKeySequence("Shift+Return"), w, activated=self.prev, context=Qt.ShortcutContext.WidgetShortcut)
        QShortcut(QKeySequence("Escape"), self, activated=self.hide_bar, context=Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.hide()
        self._changed()

    # ---- 열고 닫기
    def open(self, replace: bool = False) -> None:
        """고른 글이 있으면 찾을 글로 채우고 보여 준다."""
        picked = self.editor.textCursor().selectedText()
        if picked and "\u2029" not in picked:
            self.find_edit.setText(picked)
        self.show()
        target = self.replace_edit if replace else self.find_edit
        target.setFocus()
        target.selectAll()
        self._changed()

    def hide_bar(self) -> None:
        self.hide()
        self.editor.highlight_matches("")
        self.editor.setFocus()
        self.closed.emit()

    # ---- 상태
    @property
    def text(self) -> str:
        return self.find_edit.text()

    @property
    def case(self) -> bool:
        return self.case_check.isChecked()

    def _changed(self, *_a) -> None:
        text = self.text
        total = self.editor.count_matches(text, self.case) if text else 0
        has = bool(text)
        for b in (self.prev_btn, self.next_btn):
            b.setEnabled(has and total > 0)
        for b in (self.replace_btn, self.replace_all_btn):
            b.setEnabled(has and total > 0 and not self.editor.isReadOnly())
        self.find_edit.setProperty("invalid", bool(has and total == 0))
        self.editor.highlight_matches(text if self.isVisible() else "", self.case)
        self.find_edit.style().unpolish(self.find_edit)
        self.find_edit.style().polish(self.find_edit)
        self._update_count(total)

    def _update_count(self, total: Optional[int] = None) -> None:
        text = self.text
        if not text:
            self.count_label.setText("")
            return
        if total is None:
            total = self.editor.count_matches(text, self.case)
        if total == 0:
            self.count_label.setText("없음")
            return
        idx = self.editor.match_index(text, self.case)
        self.count_label.setText(f"{idx}/{total}" if idx else f"{total}개")

    # ---- 동작
    def next(self) -> None:
        self.editor.find_text(self.text, self.case)
        self._update_count()

    def prev(self) -> None:
        self.editor.find_text(self.text, self.case, backward=True)
        self._update_count()

    def replace(self) -> None:
        self.editor.replace_one(self.text, self.replace_edit.text(), self.case)
        self._changed()

    def replace_all(self) -> int:
        n = self.editor.replace_all(self.text, self.replace_edit.text(), self.case)
        self._changed()
        return n
