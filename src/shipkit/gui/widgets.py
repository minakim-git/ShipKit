"""여러 화면에서 쓰는 위젯과 도우미 (카드, 페이지 제목, 상태 배지, 경로 입력 등)."""
from __future__ import annotations

from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

from PySide6.QtCore import QEvent, QRectF, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QColor, QDesktopServices, QFont, QFontMetrics, QGuiApplication, QPainter, QPainterPath, QTextCursor,
)
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QFileDialog, QStyleOptionComboBox, QStylePainter, QFrame, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton, QSizePolicy, QStyle, QStyledItemDelegate,
    QStyleOptionViewItem, QVBoxLayout, QWidget,
)

from shipkit.gui import theme


def mark_invalid(widget: QWidget, message: str = "") -> None:
    """입력 칸을 잘못된 상태로(빨간 테두리 + 말풍선) 또는 정상으로 표시한다. message 가 비면 정상."""
    if hasattr(widget, "edit"):   # PathField
        widget = widget.edit
    if bool(widget.property("invalid")) == bool(message) and widget.toolTip() == message:
        return
    widget.setProperty("invalid", bool(message))
    widget.setToolTip(message)
    repolish(widget)


def label(text: str, name: str = "", wrap: bool = True) -> QLabel:
    w = QLabel(text)
    w.setWordWrap(wrap)
    if name:
        w.setObjectName(name)
    return w


class ElidedLabel(QLabel):
    """경로처럼 띄어쓰기가 없어 줄바꿈이 안 되는 글. 칸이 좁으면 가운데를 … 으로 줄이고, 전체 글은 말풍선으로 보여 준다.
    (줄바꿈이 안 되는 긴 글은 카드의 최소 폭을 밀어 올려 창 오른쪽이 잘리게 만든다.)"""

    def __init__(self, name: str = ""):
        super().__init__()
        if name:
            self.setObjectName(name)
        self.full_text = ""
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

    def setText(self, text: str) -> None:  # noqa: N802  (QLabel 이름 그대로)
        self.full_text = text
        self.setToolTip(text)
        self._update()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._update()

    def _update(self) -> None:
        fm = self.fontMetrics()
        width = max(self.width() - 4, 60)
        super().setText("\n".join(fm.elidedText(line, Qt.TextElideMode.ElideMiddle, width)
                                  for line in self.full_text.split("\n")))

    def minimumSizeHint(self):  # noqa: N802
        return QSize(80, super().minimumSizeHint().height())

    def sizeHint(self):  # noqa: N802
        fm = self.fontMetrics()
        widest = max((fm.horizontalAdvance(line) for line in self.full_text.split("\n")), default=0)
        return QSize(widest + 4, super().sizeHint().height())


def button(text: str, kind: str = "") -> QPushButton:
    """kind: '' (보조), 'primary', 'ghost', 'danger'"""
    b = QPushButton(text)
    if kind:
        b.setObjectName(kind)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    return b


def primary_button(text: str) -> QPushButton:
    return button(text, "primary")


def repolish(widget: QWidget) -> None:
    """objectName·속성을 바꾼 뒤 스타일을 다시 적용한다."""
    widget.style().unpolish(widget)
    widget.style().polish(widget)


class Chip(QLabel):
    """작은 상태 배지. tone: success / warning / danger / info / primary / neutral"""

    def __init__(self, text: str = "", tone: str = "neutral"):
        super().__init__(text)
        self.setObjectName("chip")
        self.set_tone(tone)

    def set_tone(self, tone: str) -> None:
        self.setProperty("tone", tone)
        repolish(self)

    def set(self, text: str, tone: str) -> None:
        self.setText(text)
        self.set_tone(tone)


class _ClickableHead(QWidget):
    """카드 머리: 접을 수 있는 카드에서는 어디를 눌러도 접고 편다."""

    def __init__(self, on_click: Optional[Callable[[], None]] = None):
        super().__init__()
        self.on_click = on_click
        if on_click:
            self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mousePressEvent(self, event) -> None:
        if self.on_click and event.button() == Qt.MouseButton.LeftButton:
            self.on_click()
            event.accept()
            return
        super().mousePressEvent(event)


class Card(QFrame):
    """흰 카드. title 을 주면 위에 제목(과 오른쪽 위젯)을 놓는다. 내용은 card.body 에 넣는다.

    collapsible=True 면 머리에 접기 단추가 생기고, set_collapsed() 로 내용(card.content)을 숨길 수 있다.
    """

    def __init__(self, title: str = "", subtitle: str = "", right: Optional[QWidget] = None, padding: int = 18,
                 collapsible: bool = False):
        super().__init__()
        self.setObjectName("card")
        self.collapsed = False
        self.toggle_btn: Optional[QPushButton] = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(padding, padding - 2, padding, padding)
        outer.setSpacing(10)
        if title or right or collapsible:
            head_widget = _ClickableHead(self.toggle if collapsible else None)
            head = QHBoxLayout(head_widget)
            head.setContentsMargins(0, 0, 0, 0)
            texts = QVBoxLayout()
            texts.setSpacing(2)
            if title:
                texts.addWidget(label(title, "cardTitle"))
            if subtitle:
                texts.addWidget(label(subtitle, "muted"))
            head.addLayout(texts, 1)
            if right is not None:
                head.addWidget(right, 0, Qt.AlignmentFlag.AlignTop)
            if collapsible:
                self.toggle_btn = button("", "ghost")
                self.toggle_btn.setFixedSize(28, 28)
                self.toggle_btn.setToolTip("접기 / 펼치기")
                self.toggle_btn.clicked.connect(self.toggle)
                head.addWidget(self.toggle_btn, 0, Qt.AlignmentFlag.AlignTop)
                theme.on_change(self._refresh_toggle_icon)
                self._refresh_toggle_icon()
            outer.addWidget(head_widget)
        self.content = QWidget()
        self.body = QVBoxLayout(self.content)
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(10)
        outer.addWidget(self.content, 1)

    def set_collapsed(self, collapsed: bool) -> None:
        self.collapsed = collapsed
        self.content.setVisible(not collapsed)
        self._refresh_toggle_icon()

    def toggle(self) -> None:
        self.set_collapsed(not self.collapsed)

    def _refresh_toggle_icon(self) -> None:
        if self.toggle_btn is not None:
            name = "chevron-right.svg" if self.collapsed else "chevron-down.svg"
            self.toggle_btn.setIcon(theme.svg_icon(name, "MUTED", 16))


class Banner(QFrame):
    """완료·오류 안내 띠. tone: success / danger"""

    def __init__(self, tone: str = "success"):
        super().__init__()
        self.setObjectName("banner")
        self.setProperty("tone", tone)
        self.layout_ = QHBoxLayout(self)
        self.layout_.setContentsMargins(16, 12, 16, 12)
        self.text = label("")
        fg = theme.TONES[tone][0]
        self.text.setStyleSheet(f"color: {fg}; font-weight: 500;")
        self.text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.layout_.addWidget(self.text, 1)


def page_header(title: str, subtitle: str, right: Optional[QWidget] = None) -> QWidget:
    w = QWidget()
    layout = QHBoxLayout(w)
    layout.setContentsMargins(2, 0, 2, 4)
    texts = QVBoxLayout()
    texts.setSpacing(2)
    texts.addWidget(label(title, "pageTitle"))
    sub = label(subtitle, "pageSubtitle")
    sub.setVisible(bool(subtitle))  # 설명글 없는 화면은 제목만 (채워 넣는 화면은 setVisible 로 다시 켠다)
    texts.addWidget(sub)
    layout.addLayout(texts, 1)
    if right is not None:
        layout.addWidget(right, 0, Qt.AlignmentFlag.AlignBottom)
    return w


def divider() -> QFrame:
    line = QFrame()
    line.setObjectName("divider")
    return line


class StatusDelegate(QStyledItemDelegate):
    """표의 '상태' 칸을 둥근 배지로 그린다. 항목의 UserRole 에 tone 을 넣어 둔다."""

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:
        tone = index.data(Qt.ItemDataRole.UserRole) or "neutral"
        text = index.data(Qt.ItemDataRole.DisplayRole) or ""
        fg, bg = theme.TONES.get(tone, theme.TONES["neutral"])
        # 행 배경·밑줄은 다른 칸과 똑같이 스타일시트로 그리고, 글자 대신 배지를 얹는다
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.text = ""
        style = opt.widget.style() if opt.widget else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, opt.widget)
        if not text:
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        font = QFont(option.font)
        font.setPointSizeF(8.8)
        font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)
        metrics = painter.fontMetrics()
        w = metrics.horizontalAdvance(text) + 18
        h = metrics.height() + 6
        rect = QRectF(option.rect.x() + 8, option.rect.center().y() - h / 2, w, h)
        path = QPainterPath()
        path.addRoundedRect(rect, h / 2, h / 2)
        painter.fillPath(path, QColor(bg))
        painter.setPen(QColor(fg))
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)
        painter.restore()

    def sizeHint(self, option, index):
        size = super().sizeHint(option, index)
        size.setWidth(size.width() + 26)
        return size


class Stepper(QWidget):
    """① 고르기 ── ② 확인 ── ③ 완료 같은 단계 표시. 갈 수 있는 단계는 눌러서 이동한다."""
    clicked = Signal(int)

    def __init__(self, names: Sequence[str]):
        super().__init__()
        self.names = list(names)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        self.buttons: List[QPushButton] = []
        for i, name in enumerate(self.names):
            if i:
                line = QFrame()
                line.setObjectName("stepLine")
                line.setFixedSize(24, 2)
                row.addWidget(line)
            b = QPushButton(f"{i + 1}  {name}")
            b.setObjectName("step")
            b.setMinimumWidth(86)  # 번호가 ✓ 로 바뀌어도 폭이 흔들리지 않게
            b.clicked.connect(lambda _=False, i=i: self.clicked.emit(i))
            row.addWidget(b)
            self.buttons.append(b)

    def set_state(self, current: int, reachable: int) -> None:
        for i, b in enumerate(self.buttons):
            state = "current" if i == current else ("done" if i < current else "todo")
            b.setText(f"{'✓' if state == 'done' else i + 1}  {self.names[i]}")
            b.setProperty("state", state)
            b.setEnabled(i != current and i <= reachable)
            b.setCursor(Qt.CursorShape.PointingHandCursor if b.isEnabled() else Qt.CursorShape.ArrowCursor)
            b.style().unpolish(b)
            b.style().polish(b)


class CommandRow(QFrame):
    """서버에서 실행할 명령 한 줄: 번호 · 명령 · 설명 · [복사]"""

    def __init__(self, number: str, command: str, description: str):
        super().__init__()
        self.setObjectName("cmdRow")
        self.command = command
        row = QHBoxLayout(self)
        row.setContentsMargins(10, 4, 6, 4)
        row.setSpacing(12)
        num = QLabel(number)
        num.setObjectName("stepNum")
        num.setAlignment(Qt.AlignmentFlag.AlignCenter)
        num.setFixedSize(24, 24)
        code = QLabel(command)
        code.setObjectName("code")
        code.setWordWrap(True)  # 긴 명령·설명이 줄바꿈되지 않으면 카드가 창보다 넓어져 오른쪽이 잘린다
        code.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.copy_btn = button("복사", "ghost")
        theme.bind_icon(self.copy_btn, "copy.svg", "PRIMARY", 16)
        self.copy_btn.clicked.connect(self.copy)
        row.addWidget(num)
        row.addWidget(code, 2)
        row.addSpacing(8)
        row.addWidget(label(description, "muted"), 1)
        row.addWidget(self.copy_btn)

    def copy(self) -> None:
        QGuiApplication.clipboard().setText(self.command)
        self.copy_btn.setText("복사됨")
        QTimer.singleShot(1500, lambda: self.copy_btn.setText("복사"))


META_ROLE = Qt.ItemDataRole.UserRole + 1   # 프로젝트 전환기 항목의 두 번째 줄 (프리셋 · 서버)
# 프로젝트 전환기의 머리글자 동그라미 색: 아이보리 바탕에 맞춰 채도를 낮춘 색들
_AVATAR_COLORS = ("#8B8468", "#5B7590", "#B4533E", "#A3692B", "#4F7A4A", "#7D6B8E", "#3F7A76", "#9C5A7A")


def _avatar_color(name: str) -> QColor:
    return QColor(_AVATAR_COLORS[sum(map(ord, name)) % len(_AVATAR_COLORS)])


def _draw_avatar(painter: QPainter, rect: QRectF, name: str) -> None:
    """프로젝트 이름 첫 글자를 넣은 둥근 사각형 (프로젝트마다 색이 다르다)."""
    path = QPainterPath()
    path.addRoundedRect(rect, 7, 7)
    painter.fillPath(path, _avatar_color(name))
    font = QFont(painter.font())
    font.setPointSizeF(10.5)
    font.setWeight(QFont.Weight.Bold)
    painter.setFont(font)
    painter.setPen(QColor("white"))
    painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, (name[:1] or "?").upper())


def _draw_two_lines(painter: QPainter, rect: QRectF, title: str, meta: str, base: QFont,
                    title_color: str = "TEXT") -> None:
    title_font = QFont(base)
    title_font.setWeight(QFont.Weight.DemiBold)
    meta_font = QFont(base)
    meta_font.setPointSizeF(8.6)
    half = rect.height() / 2
    top = QRectF(rect.x(), rect.y(), rect.width(), half if meta else rect.height())
    painter.setFont(title_font)
    painter.setPen(QColor(theme.color(title_color)))
    align = Qt.AlignmentFlag.AlignLeft | (Qt.AlignmentFlag.AlignBottom if meta else Qt.AlignmentFlag.AlignVCenter)
    painter.drawText(top.adjusted(0, 0, 0, -1), align,
                     QFontMetrics(title_font).elidedText(title, Qt.TextElideMode.ElideMiddle, int(rect.width())))
    if meta:
        bottom = QRectF(rect.x(), rect.y() + half, rect.width(), half)
        painter.setFont(meta_font)
        painter.setPen(QColor(theme.MUTED))
        painter.drawText(bottom.adjusted(0, 1, 0, 0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                         QFontMetrics(meta_font).elidedText(meta, Qt.TextElideMode.ElideRight, int(rect.width())))


class _SwitcherDelegate(QStyledItemDelegate):
    """펼친 목록: 프로젝트는 [첫 글자] 이름 / 프리셋 · 서버 두 줄, 동작 항목(추가·가져오기)은 강조색 한 줄."""

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:
        if index.data(Qt.ItemDataRole.AccessibleDescriptionRole) == "separator":
            painter.save()
            painter.setPen(QColor(theme.BORDER))
            y = option.rect.center().y()
            painter.drawLine(option.rect.left() + 8, y, option.rect.right() - 8, y)
            painter.restore()
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(option.rect).adjusted(4, 2, -4, -2)
        hovered = option.state & (QStyle.StateFlag.State_Selected | QStyle.StateFlag.State_MouseOver)
        if hovered:
            path = QPainterPath()
            path.addRoundedRect(rect, 8, 8)
            painter.fillPath(path, QColor(theme.HOVER))
        text = index.data(Qt.ItemDataRole.DisplayRole) or ""
        meta = index.data(META_ROLE)
        if meta is None:  # 동작 항목
            _draw_two_lines(painter, rect.adjusted(12, 0, -8, 0), text, "", option.font, "PRIMARY")
        else:
            _draw_avatar(painter, QRectF(rect.x() + 8, rect.center().y() - 14, 28, 28), text)
            _draw_two_lines(painter, rect.adjusted(46, 4, -8, -4), text, meta, option.font)
        painter.restore()

    def sizeHint(self, option, index):
        size = super().sizeHint(option, index)
        if index.data(Qt.ItemDataRole.AccessibleDescriptionRole) == "separator":
            size.setHeight(9)
        else:
            size.setHeight(52 if index.data(META_ROLE) is not None else 38)
        return size


class ProjectSwitcher(QComboBox):
    """왼쪽 위 작업할 프로젝트 고르기. 닫힌 상태는 [첫 글자] 이름 / 프리셋 · 서버 카드로 보이고,
    이름이 길면 가운데를 '…'로 줄인다. 펼친 목록에서는 전체 이름이 보인다."""

    def __init__(self):
        super().__init__()
        self.setItemDelegate(_SwitcherDelegate(self))
        self.setMinimumHeight(58)

    def add_project(self, name: str, meta: str) -> None:
        self.addItem(name, name)
        self.setItemData(self.count() - 1, meta, META_ROLE)

    def paintEvent(self, event) -> None:
        painter = QStylePainter(self)
        opt = QStyleOptionComboBox()
        self.initStyleOption(opt)
        opt.currentText = ""
        painter.drawComplexControl(QStyle.ComplexControl.CC_ComboBox, opt)  # 테두리·배경·화살표 (스타일시트)
        field = QRectF(self.style().subControlRect(QStyle.ComplexControl.CC_ComboBox, opt,
                                                   QStyle.SubControl.SC_ComboBoxEditField, self))
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        index = self.currentIndex()
        if index < 0:
            _draw_two_lines(painter, field.adjusted(4, 0, 0, 0), self.placeholderText() or "프로젝트 없음", "",
                            self.font(), "MUTED")
            return
        name = self.itemText(index)
        meta = self.itemData(index, META_ROLE) or ""
        _draw_avatar(painter, QRectF(field.x() + 2, field.center().y() - 15, 30, 30), name)
        _draw_two_lines(painter, field.adjusted(42, 8, -2, -8), name, meta, self.font())

    def showPopup(self) -> None:
        widest = max((self.fontMetrics().horizontalAdvance(self.itemText(i)) for i in range(self.count())), default=0)
        self.view().setMinimumWidth(max(self.width(), widest + 90))  # 펼친 목록에서는 전체 이름
        super().showPopup()


class DropFileList(QPlainTextEdit):
    """파일 목록 입력칸. 탐색기에서 파일·폴더를 끌어다 놓으면(또는 붙여 넣으면) 저장소 기준 상대 경로로 바꿔 한 줄씩 넣는다.
    폴더는 안의 파일을 모두 넣고, 저장소 밖 파일은 빼고 알린다. added(넣은 수, 저장소 밖 경로 목록) 신호."""
    added = Signal(int, list)
    LIMIT = 2000   # 폴더를 떨어뜨렸을 때 넣는 최대 파일 수

    def __init__(self, root_provider: Callable[[], Optional[Path]]):
        super().__init__()
        self.root_provider = root_provider
        self.setAcceptDrops(True)

    def canInsertFromMimeData(self, source) -> bool:  # noqa: N802
        return source.hasUrls() or super().canInsertFromMimeData(source)

    def insertFromMimeData(self, source) -> None:  # noqa: N802
        if source.hasUrls():
            self.add_paths([u.toLocalFile() for u in source.urls() if u.isLocalFile()])
            return
        super().insertFromMimeData(source)

    def add_paths(self, paths: Sequence[str]) -> Tuple[int, List[str]]:
        """절대 경로들을 저장소 기준 상대 경로로 바꿔 목록 끝에 더한다. 반환: (넣은 수, 저장소 밖 경로)."""
        root = self.root_provider()
        existing = {line.strip() for line in self.toPlainText().splitlines() if line.strip()}
        lines: List[str] = []
        outside: List[str] = []
        for raw in paths:
            p = Path(raw)
            files = [p]
            if p.is_dir():
                files = sorted((f for f in p.rglob("*") if f.is_file()), key=lambda f: f.as_posix())[: self.LIMIT]
            for f in files:
                try:
                    rel = f.resolve().relative_to(root.resolve()).as_posix() if root else None
                except (ValueError, OSError):
                    rel = None
                if rel is None:
                    outside.append(str(f))
                    continue
                if rel not in existing:
                    existing.add(rel)
                    lines.append(rel)
        if lines:
            text = self.toPlainText()
            if text and not text.endswith("\n"):
                text += "\n"
            self.setPlainText(text + "\n".join(lines) + "\n")
            self.moveCursor(QTextCursor.MoveOperation.End)
        self.added.emit(len(lines), outside)
        return len(lines), outside


class PathField(QWidget):
    """경로 입력칸 + [찾아보기] 버튼."""

    def __init__(self, folder: bool = True, placeholder: str = "", file_filter: str = ""):
        super().__init__()
        self.folder = folder
        self.file_filter = file_filter
        self.edit = QLineEdit()
        self.edit.setPlaceholderText(placeholder)
        browse = button("찾아보기")
        browse.clicked.connect(self._browse)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(self.edit, 1)
        layout.addWidget(browse)

    def _browse(self) -> None:
        start = self.edit.text().strip() or str(Path.home())
        if self.folder:
            chosen = QFileDialog.getExistingDirectory(self, "폴더 선택", start)
        else:
            chosen, _ = QFileDialog.getOpenFileName(self, "파일 선택", start, self.file_filter)
        if chosen:
            self.edit.setText(chosen.replace("\\", "/"))

    def text(self) -> str:
        return self.edit.text().strip()

    def setText(self, value: str) -> None:
        self.edit.setText(value)


def open_path(path: Path) -> None:
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))


def open_location(location: str) -> None:
    """폴더(로컬·공유)는 탐색기로, URL 은 브라우저로 연다. version.txt 파일을 가리키면 그 폴더를 연다."""
    if location.lower().startswith(("http://", "https://")):
        QDesktopServices.openUrl(QUrl(location))
        return
    path = Path(location)
    open_path(path.parent if path.suffix.lower() == ".txt" else path)


_MESSAGE_MARK = {"info": "i", "success": "✓", "warning": "!", "danger": "!", "question": "?"}


class MessageDialog(QDialog):
    """앱 안의 다른 창과 같은 모양의 메시지 창 (Qt 기본 QMessageBox 는 아이콘 칸이 넓고 글이 오른쪽에 몰리며 단추가 영어다).

    왼쪽에 작은 색 동그라미 표시, 오른쪽에 제목과 글. 단추는 오른쪽 아래, 마지막 단추가 기본(Enter). Esc 는 거절.
    buttons: (글, 받아들임 여부) 목록. 예: [("아니요", False), ("예", True)]
    """

    def __init__(self, parent: Optional[QWidget], title: str, text: str, tone: str = "info",
                 buttons: Sequence[Tuple[str, bool]] = (("확인", True),), extra: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(480)
        self.setMaximumWidth(680)
        self.clicked: Optional[int] = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 16)
        layout.setSpacing(16)
        body = QHBoxLayout()
        body.setSpacing(14)
        mark = QLabel(_MESSAGE_MARK.get(tone, "i"))
        mark.setFixedSize(30, 30)
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        color = theme.TONES.get("primary" if tone == "question" else tone, theme.TONES["info"])[0]
        mark.setStyleSheet(f"background: {color}; color: white; border-radius: 15px; font-weight: 700; font-size: 12pt;")
        body.addWidget(mark, 0, Qt.AlignmentFlag.AlignTop)
        texts = QVBoxLayout()
        texts.setSpacing(6)
        texts.addWidget(label(title, "cardTitle"))
        self.text = label(text)
        self.text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        texts.addWidget(self.text)
        if extra is not None:
            texts.addWidget(extra)
        body.addLayout(texts, 1)
        layout.addLayout(body)
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addStretch(1)
        self.buttons: List[QPushButton] = []
        for i, (caption, accept) in enumerate(buttons):
            b = button(caption, "primary" if accept else "")
            b.setMinimumWidth(84)
            b.clicked.connect(lambda _=False, index=i, ok=accept: self._pick(index, ok))
            row.addWidget(b)
            self.buttons.append(b)
        if self.buttons:
            self.buttons[-1].setDefault(True)
        layout.addLayout(row)

    def _pick(self, index: int, accept: bool) -> None:
        self.clicked = index
        self.accept() if accept else self.reject()


class Toast(QFrame):
    """창 아래 가운데에 잠깐 떠 있다 사라지는 알림. 확인을 누를 필요가 없는 소식(저장했습니다 등)에 쓴다.
    창마다 하나를 두고, 새 소식이 오면 글만 바꿔 다시 띄운다."""

    def __init__(self, window: QWidget):
        super().__init__(window)
        self.setObjectName("toast")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        row = QHBoxLayout(self)
        row.setContentsMargins(14, 9, 18, 9)
        row.setSpacing(10)
        self.mark = QLabel()
        self.mark.setFixedSize(18, 18)
        self.mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.text = QLabel()
        self.text.setObjectName("toastText")
        row.addWidget(self.mark)
        row.addWidget(self.text)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.hide)
        window.installEventFilter(self)
        self.hide()

    def show_message(self, text: str, kind: str = "success") -> None:
        color = theme.TONES.get(kind, theme.TONES["info"])[0]
        self.mark.setText(_MESSAGE_MARK.get(kind, "i"))
        self.mark.setStyleSheet(f"background: {color}; color: white; border-radius: 9px; font-weight: 700; font-size: 8pt;")
        self.text.setText(text)
        self._place()
        self.show()
        self.raise_()
        self.timer.start(2400 + 30 * len(text))  # 긴 글은 조금 더 오래

    def _place(self) -> None:
        self.adjustSize()
        parent = self.parentWidget()
        self.move((parent.width() - self.width()) // 2, parent.height() - self.height() - 26)

    def eventFilter(self, obj, event) -> bool:
        if event.type() == QEvent.Type.Resize and self.isVisible():
            self._place()
        return False


class Dialogs:
    """메시지 창. 테스트에서는 창을 띄우지 않는 대역으로 바꿔 끼운다."""

    def __init__(self, parent: Optional[QWidget] = None):
        self.parent = parent

    def info(self, title: str, text: str) -> None:
        MessageDialog(self.parent, title, text, "info").exec()

    def toast(self, text: str, kind: str = "success") -> None:
        """확인이 필요 없는 짧은 소식: 창 대신 아래쪽에 잠깐 띄운다. kind: success / info / warning."""
        window = self.parent.window() if self.parent is not None else None
        if window is None:
            MessageDialog(self.parent, "알림", text, "info").exec()
            return
        toast = getattr(window, "_shipkit_toast", None)
        if toast is None:
            toast = Toast(window)
            window._shipkit_toast = toast
        toast.show_message(text, kind)

    def error(self, title: str, text: str) -> None:
        MessageDialog(self.parent, title, text, "danger").exec()

    def ask(self, title: str, text: str) -> bool:
        return bool(MessageDialog(self.parent, title, text, "question", [("아니요", False), ("예", True)]).exec())

    def choose(self, title: str, text: str, options: Sequence[str]) -> Optional[str]:
        """목록에서 하나 고르기. 취소하면 None."""
        combo = QComboBox()
        combo.addItems(list(options))
        dialog = MessageDialog(self.parent, title, text, "question", [("취소", False), ("확인", True)], extra=combo)
        return combo.currentText() if dialog.exec() else None

    def ask_secret(self, title: str, text: str) -> Optional[Tuple[str, bool]]:
        """비밀번호 입력 + [이 PC 에 저장]. 반환: (값, 저장 여부) 또는 취소 시 None."""
        from PySide6.QtWidgets import QCheckBox
        extra = QWidget()
        col = QVBoxLayout(extra)
        col.setContentsMargins(0, 4, 0, 0)
        col.setSpacing(8)
        edit = QLineEdit()
        edit.setEchoMode(QLineEdit.EchoMode.Password)
        save = QCheckBox("이 PC 에 저장 (Windows 자격 증명 관리자, 다음부터 묻지 않음)")
        save.setChecked(True)
        col.addWidget(edit)
        col.addWidget(save)
        dialog = MessageDialog(self.parent, title, text, "question", [("취소", False), ("확인", True)], extra=extra)
        edit.setFocus()
        if not dialog.exec():
            return None
        return edit.text(), save.isChecked()

    def password(self, title: str, text: str) -> Optional[str]:
        edit = QLineEdit()
        edit.setEchoMode(QLineEdit.EchoMode.Password)
        dialog = MessageDialog(self.parent, title, text, "question", [("취소", False), ("확인", True)], extra=edit)
        edit.setFocus()
        return edit.text().strip() if dialog.exec() else None
