"""터미널 위젯: 서버 셸(pty)의 출력을 pyte(터미널 에뮬레이터)로 해석해 글자 격자로 그리고, 키 입력을 바이트로 보낸다.

- 받은 바이트는 data_in 신호로 넣는다 (읽기 스레드에서 emit 해도 화면 스레드에서 처리된다).
- 사용자가 친 키는 input_bytes 신호로 나간다 (서버 화면이 셸에 보낸다). 크기가 바뀌면 resized(열, 줄).
- 마우스로 끌어 고르고 Ctrl+Shift+C 로 복사, Ctrl+Shift+V · Shift+Insert · 오른쪽 단추로 붙여넣기.
  Shift+PageUp/PageDown · 휠로 지난 출력을 본다. 색·굵게·밑줄·반전을 그린다. 한글 같은 넓은 글자는 두 칸.
"""
from __future__ import annotations

from typing import Optional, Tuple

import pyte
from PySide6.QtCore import QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QGuiApplication, QPainter
from PySide6.QtWidgets import QMenu, QWidget

from shipkit.gui import theme

MONO = ["Cascadia Mono", "Consolas", "Malgun Gothic"]
HISTORY = 5000

# 어두운 바탕의 고정 팔레트 (앱 테마와 상관없이 터미널답게)
PALETTE = {
    "bg": "#101827", "fg": "#e2e8f0", "cursor": "#facc15", "selection": "#2b4a7a",
    "black": "#1e293b", "red": "#f87171", "green": "#4ade80", "brown": "#facc15", "yellow": "#facc15",
    "blue": "#60a5fa", "magenta": "#c084fc", "cyan": "#22d3ee", "white": "#e2e8f0",
    "brightblack": "#64748b", "brightred": "#fca5a5", "brightgreen": "#86efac", "brightbrown": "#fde047",
    "brightyellow": "#fde047", "brightblue": "#93c5fd", "brightmagenta": "#d8b4fe", "brightcyan": "#67e8f9",
    "brightwhite": "#f8fafc",
}

# 키 → 서버로 보낼 바이트 (xterm)
_KEYS = {
    Qt.Key.Key_Return: b"\r", Qt.Key.Key_Enter: b"\r", Qt.Key.Key_Backspace: b"\x7f", Qt.Key.Key_Tab: b"\t",
    Qt.Key.Key_Escape: b"\x1b", Qt.Key.Key_Up: b"\x1b[A", Qt.Key.Key_Down: b"\x1b[B", Qt.Key.Key_Right: b"\x1b[C",
    Qt.Key.Key_Left: b"\x1b[D", Qt.Key.Key_Home: b"\x1b[H", Qt.Key.Key_End: b"\x1b[F", Qt.Key.Key_Insert: b"\x1b[2~",
    Qt.Key.Key_Delete: b"\x1b[3~", Qt.Key.Key_PageUp: b"\x1b[5~", Qt.Key.Key_PageDown: b"\x1b[6~",
    Qt.Key.Key_F1: b"\x1bOP", Qt.Key.Key_F2: b"\x1bOQ", Qt.Key.Key_F3: b"\x1bOR", Qt.Key.Key_F4: b"\x1bOS",
    Qt.Key.Key_F5: b"\x1b[15~", Qt.Key.Key_F6: b"\x1b[17~", Qt.Key.Key_F7: b"\x1b[18~", Qt.Key.Key_F8: b"\x1b[19~",
    Qt.Key.Key_F9: b"\x1b[20~", Qt.Key.Key_F10: b"\x1b[21~", Qt.Key.Key_F11: b"\x1b[23~", Qt.Key.Key_F12: b"\x1b[24~",
}


def _color(value: str, fallback: str) -> QColor:
    """pyte 의 색 이름(red, brightblue…) 또는 16진수(ff8800)를 QColor 로."""
    if value == "default":
        return QColor(fallback)
    if value in PALETTE:
        return QColor(PALETTE[value])
    if len(value) == 6:
        return QColor("#" + value)
    return QColor(fallback)


class TerminalView(QWidget):
    data_in = Signal(bytes)        # 서버가 보낸 바이트 (어느 스레드에서 emit 해도 된다)
    closed = Signal()              # 셸이 끝났을 때 (읽기 스레드에서 emit)
    input_bytes = Signal(bytes)    # 사용자가 친 것 → 셸로
    resized = Signal(int, int)     # (열, 줄) 이 바뀌었을 때 → 셸의 pty 크기

    def __init__(self, cols: int = 80, rows: int = 24):
        super().__init__()
        self.cols, self.rows = cols, rows
        self.screen = pyte.HistoryScreen(cols, rows, history=HISTORY)
        self.stream = pyte.ByteStream(self.screen)
        self.selection: Optional[Tuple[Tuple[int, int], Tuple[int, int]]] = None   # ((줄, 칸), (줄, 칸))
        self._selecting = False
        self._preedit = ""
        self.cell_w = self.cell_h = 1.0
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled, True)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setCursor(Qt.CursorShape.IBeamCursor)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.DefaultContextMenu)
        self.data_in.connect(self.feed)
        self.apply_font()

    # ------------------------------------------------------------------ 글꼴·크기
    def apply_font(self) -> None:
        font = QFont()
        font.setFamilies(MONO)
        font.setPointSizeF(10 * theme.FONT_SCALE)
        font.setFixedPitch(True)
        self.setFont(font)
        fm = QFontMetricsF(font)
        self.cell_w = max(fm.horizontalAdvance("M"), 1.0)
        self.cell_h = max(fm.height(), 1.0)
        self._ascent = fm.ascent()
        self._fit()
        self.update()

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(int(self.cols * self.cell_w) + 8, int(self.rows * self.cell_h) + 8)

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return QSize(int(20 * self.cell_w) + 8, int(5 * self.cell_h) + 8)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._fit()

    def _fit(self) -> None:
        cols = max(20, int((self.width() - 8) / self.cell_w))
        rows = max(5, int((self.height() - 8) / self.cell_h))
        if (cols, rows) != (self.cols, self.rows):
            self.cols, self.rows = cols, rows
            self.screen.resize(rows, cols)
            self.selection = None
            self.resized.emit(cols, rows)
            self.update()

    # ------------------------------------------------------------------ 입출력
    def feed(self, data: bytes) -> None:
        self.stream.feed(bytes(data))
        self.update()

    def text(self) -> str:
        """지금 보이는 화면 글 (줄 끝 공백과 아래 빈 줄은 뺀다). 테스트·복사용."""
        return "\n".join(line.rstrip() for line in self.screen.display).rstrip("\n")

    def send(self, data: bytes) -> None:
        if data:
            self.input_bytes.emit(data)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        key, mods, text = event.key(), event.modifiers(), event.text()
        ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
        shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)
        if ctrl and shift and key == Qt.Key.Key_C:
            self.copy_selection()
            return
        if (ctrl and shift and key == Qt.Key.Key_V) or (shift and key == Qt.Key.Key_Insert):
            self.paste()
            return
        if shift and key == Qt.Key.Key_PageUp:
            self.screen.prev_page()
            self.update()
            return
        if shift and key == Qt.Key.Key_PageDown:
            self.screen.next_page()
            self.update()
            return
        if key in _KEYS:
            self.send(_KEYS[key])
            self._scroll_to_bottom()
            return
        if ctrl and Qt.Key.Key_A <= key <= Qt.Key.Key_Z:
            self.send(bytes([key - Qt.Key.Key_A + 1]))   # Ctrl+C = 0x03 …
            self._scroll_to_bottom()
            return
        if ctrl and key in (Qt.Key.Key_BracketLeft, Qt.Key.Key_BracketRight, Qt.Key.Key_Backslash):
            self.send({Qt.Key.Key_BracketLeft: b"\x1b", Qt.Key.Key_BracketRight: b"\x1d", Qt.Key.Key_Backslash: b"\x1c"}[key])
            return
        if text and not ctrl:
            if mods & Qt.KeyboardModifier.AltModifier:
                self.send(b"\x1b" + text.encode("utf-8"))
            else:
                self.send(text.encode("utf-8"))
            self._scroll_to_bottom()
            return
        super().keyPressEvent(event)

    def _scroll_to_bottom(self) -> None:
        while self.screen.history.position < self.screen.history.size:
            before = self.screen.history.position
            self.screen.next_page()
            if self.screen.history.position == before:
                break

    def inputMethodEvent(self, event) -> None:  # noqa: N802
        commit = event.commitString()
        self._preedit = event.preeditString()
        if commit:
            self.send(commit.encode("utf-8"))
        self.update()

    def inputMethodQuery(self, query):  # noqa: N802
        if query == Qt.InputMethodQuery.ImCursorRectangle:
            x, y = self.screen.cursor.x, self.screen.cursor.y
            return QRect(int(4 + x * self.cell_w), int(4 + y * self.cell_h), int(self.cell_w), int(self.cell_h))
        return super().inputMethodQuery(query)

    def paste(self) -> None:
        text = QGuiApplication.clipboard().text()
        if text:
            self.send(text.replace("\r\n", "\r").replace("\n", "\r").encode("utf-8"))
            self._scroll_to_bottom()

    # ------------------------------------------------------------------ 고르기·복사
    def _cell_at(self, pos) -> Tuple[int, int]:
        col = min(max(int((pos.x() - 4) / self.cell_w), 0), self.cols - 1)
        row = min(max(int((pos.y() - 4) / self.cell_h), 0), self.rows - 1)
        return row, col

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            cell = self._cell_at(event.position())
            self.selection = (cell, cell)
            self._selecting = True
            self.update()
        elif event.button() == Qt.MouseButton.MiddleButton:
            self.paste()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._selecting and self.selection is not None:
            self.selection = (self.selection[0], self._cell_at(event.position()))
            self.update()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self._selecting:
            self._selecting = False
            if self.selection is not None and self.selection[0] == self.selection[1]:
                self.selection = None   # 클릭만 한 것
                self.update()
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        row, col = self._cell_at(event.position())
        line = self.screen.display[row] if row < len(self.screen.display) else ""
        if col < len(line) and not line[col].isspace():
            start = col
            while start > 0 and not line[start - 1].isspace():
                start -= 1
            end = col
            while end + 1 < len(line) and not line[end + 1].isspace():
                end += 1
            self.selection = ((row, start), (row, end))
            self.update()

    def wheelEvent(self, event) -> None:  # noqa: N802
        delta = event.angleDelta().y()
        if delta > 0:
            self.screen.prev_page()
        elif delta < 0:
            self.screen.next_page()
        self.update()

    def _selected_range(self) -> Optional[Tuple[Tuple[int, int], Tuple[int, int]]]:
        if self.selection is None:
            return None
        a, b = self.selection
        return (a, b) if a <= b else (b, a)

    def selected_text(self) -> str:
        rng = self._selected_range()
        if rng is None:
            return ""
        (r1, c1), (r2, c2) = rng
        lines = []
        for row in range(r1, r2 + 1):
            line = self.screen.display[row] if row < len(self.screen.display) else ""
            start = c1 if row == r1 else 0
            end = c2 + 1 if row == r2 else self.cols
            lines.append(line[start:end].rstrip())
        return "\n".join(lines)

    def copy_selection(self) -> bool:
        text = self.selected_text()
        if text:
            QGuiApplication.clipboard().setText(text)
        return bool(text)

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        menu = QMenu(self)
        copy = menu.addAction("복사  (Ctrl+Shift+C)")
        copy.setEnabled(self.selection is not None)
        paste = menu.addAction("붙여넣기  (Ctrl+Shift+V)")
        chosen = menu.exec(event.globalPos())
        if chosen is copy:
            self.copy_selection()
        elif chosen is paste:
            self.paste()

    # ------------------------------------------------------------------ 그리기
    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(PALETTE["bg"]))
        p.setFont(self.font())
        base_font = self.font()
        bold_font = QFont(base_font)
        bold_font.setBold(True)
        cw, ch = self.cell_w, self.cell_h
        rng = self._selected_range()
        buffer = self.screen.buffer
        for y in range(self.rows):
            line = buffer[y]
            x = 0
            while x < self.cols:
                c = line[x]
                data = c.data
                if data == "":
                    x += 1
                    continue
                wide = x + 1 < self.cols and line[x + 1].data == "" and data != " "
                width = 2 if wide else 1
                fg = _color(c.fg, PALETTE["fg"])
                bg = _color(c.bg, PALETTE["bg"])
                if c.reverse:
                    fg, bg = bg, fg
                selected = rng is not None and (rng[0] <= (y, x) <= rng[1])
                if selected:
                    bg = QColor(PALETTE["selection"])
                rect = QRect(int(4 + x * cw), int(4 + y * ch), int(width * cw + 0.999), int(ch + 0.999))
                if bg.name() != PALETTE["bg"] or selected:
                    p.fillRect(rect, bg)
                if data != " ":
                    p.setPen(fg)
                    p.setFont(bold_font if c.bold else base_font)
                    p.drawText(int(4 + x * cw), int(4 + y * ch + self._ascent), data)
                    if c.underscore:
                        p.drawLine(rect.left(), rect.bottom() - 1, rect.right(), rect.bottom() - 1)
                x += width
        cur = self.screen.cursor
        if not cur.hidden and self.screen.history.position >= self.screen.history.size:
            rect = QRect(int(4 + cur.x * cw), int(4 + cur.y * ch), int(cw + 0.999), int(ch + 0.999))
            if self.hasFocus():
                p.fillRect(rect, QColor(PALETTE["cursor"]))
                under = buffer[cur.y][cur.x].data
                if under and under != " ":
                    p.setPen(QColor(PALETTE["bg"]))
                    p.setFont(base_font)
                    p.drawText(rect.left(), int(rect.top() + self._ascent), under)
            else:
                p.setPen(QColor(PALETTE["cursor"]))
                p.drawRect(rect.adjusted(0, 0, -1, -1))
            if self._preedit:   # 한글 조합 중인 글자를 커서 자리에
                p.setPen(QColor(PALETTE["fg"]))
                p.setFont(base_font)
                p.fillRect(QRect(rect.left(), rect.top(), int(2 * cw), int(ch)), QColor(PALETTE["selection"]))
                p.drawText(rect.left(), int(rect.top() + self._ascent), self._preedit)
        p.end()
