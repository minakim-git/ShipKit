"""여러 화면에서 쓰는 위젯과 도우미 (카드, 페이지 제목, 상태 배지, 경로 입력 등)."""
from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Sequence

from PySide6.QtCore import QRectF, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFont, QGuiApplication, QPainter, QPainterPath
from PySide6.QtWidgets import (
    QApplication, QComboBox, QFileDialog, QStyleOptionComboBox, QStylePainter, QFrame, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QStyle, QStyledItemDelegate,
    QStyleOptionViewItem, QVBoxLayout, QWidget,
)

from shipkit.gui import theme


def label(text: str, name: str = "", wrap: bool = True) -> QLabel:
    w = QLabel(text)
    w.setWordWrap(wrap)
    if name:
        w.setObjectName(name)
    return w


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


class Card(QFrame):
    """흰 카드. title 을 주면 위에 제목(과 오른쪽 위젯)을 놓는다. 내용은 card.body 에 넣는다."""

    def __init__(self, title: str = "", subtitle: str = "", right: Optional[QWidget] = None, padding: int = 18):
        super().__init__()
        self.setObjectName("card")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(padding, padding - 2, padding, padding)
        outer.setSpacing(10)
        if title or right:
            head = QHBoxLayout()
            texts = QVBoxLayout()
            texts.setSpacing(2)
            if title:
                texts.addWidget(label(title, "cardTitle"))
            if subtitle:
                texts.addWidget(label(subtitle, "muted"))
            head.addLayout(texts, 1)
            if right is not None:
                head.addWidget(right, 0, Qt.AlignmentFlag.AlignTop)
            outer.addLayout(head)
        self.body = QVBoxLayout()
        self.body.setSpacing(10)
        outer.addLayout(self.body, 1)


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
    texts.addWidget(label(subtitle, "pageSubtitle"))
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
        code.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.copy_btn = button("복사", "ghost")
        self.copy_btn.setIcon(theme.svg_icon("copy.svg", theme.PRIMARY, 16))
        self.copy_btn.clicked.connect(self.copy)
        row.addWidget(num)
        row.addWidget(code)
        row.addSpacing(8)
        row.addWidget(label(description, "muted", wrap=False), 1)
        row.addWidget(self.copy_btn)

    def copy(self) -> None:
        QGuiApplication.clipboard().setText(self.command)
        self.copy_btn.setText("복사됨")
        QTimer.singleShot(1500, lambda: self.copy_btn.setText("복사"))


class ElidedComboBox(QComboBox):
    """고른 글자가 칸보다 길면 가운데를 '…'로 줄여 보여 준다 (예: harami_gro…kepcomcs)."""

    def paintEvent(self, event) -> None:
        painter = QStylePainter(self)
        opt = QStyleOptionComboBox()
        self.initStyleOption(opt)
        painter.drawComplexControl(QStyle.ComplexControl.CC_ComboBox, opt)
        field = self.style().subControlRect(QStyle.ComplexControl.CC_ComboBox, opt,
                                            QStyle.SubControl.SC_ComboBoxEditField, self)
        opt.currentText = self.fontMetrics().elidedText(opt.currentText, Qt.TextElideMode.ElideMiddle, field.width() - 4)
        painter.drawControl(QStyle.ControlElement.CE_ComboBoxLabel, opt)

    def showPopup(self) -> None:
        widest = max((self.fontMetrics().horizontalAdvance(self.itemText(i)) for i in range(self.count())), default=0)
        self.view().setMinimumWidth(max(self.width(), widest + 48))  # 펼친 목록에서는 전체 이름
        super().showPopup()


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


class Dialogs:
    """메시지 창. 테스트에서는 창을 띄우지 않는 대역으로 바꿔 끼운다."""

    def __init__(self, parent: Optional[QWidget] = None):
        self.parent = parent

    def info(self, title: str, text: str) -> None:
        QMessageBox.information(self.parent, title, text)

    def error(self, title: str, text: str) -> None:
        QMessageBox.critical(self.parent, title, text)

    def ask(self, title: str, text: str) -> bool:
        return QMessageBox.question(self.parent, title, text) == QMessageBox.StandardButton.Yes

    def password(self, title: str, text: str) -> Optional[str]:
        from PySide6.QtWidgets import QInputDialog
        value, ok = QInputDialog.getText(self.parent, title, text, QLineEdit.EchoMode.Password)
        return value.strip() if ok else None
