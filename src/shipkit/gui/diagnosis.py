"""연결 진단 결과 창: [연결 시험]이 서버에서 확인한 항목(접속·실행 계정·올릴 위치·배포 경로·명령·로그 파일)을
✓/△/✗ 로 보여 주고, 설정을 고치면 되는 것은 [추천 설정 적용]으로 서버 연결 폼에 바로 넣는다."""
from __future__ import annotations

import textwrap
from typing import Callable, Dict, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QHBoxLayout, QHeaderView, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget

from shipkit import remote
from shipkit.gui import theme
from shipkit.gui.widgets import Chip, button, label

MARK = {"ok": ("✓ 정상", "success"), "warn": ("△ 확인", "warning"), "fail": ("✗ 실패", "danger"), "info": ("· 참고", "neutral")}
SUGGESTION_TEXT = {
    ("run_with", "sudo"): "전환 방법: sudo -u 계정", ("run_with", "su"): "전환 방법: su - 계정",
    ("run_password", True): "실행할 때 비밀번호가 필요합니다: 켬", ("run_password", False): "실행할 때 비밀번호가 필요합니다: 끔",
    ("chown", True): "올린 패키지 폴더 소유자 변경(chown): 켬", ("chown", False): "올린 패키지 폴더 소유자 변경(chown): 끔",
}


def _wrap(text: str, width: int = 62) -> str:
    """항목 뷰는 자동 줄바꿈을 못 하므로 긴 글은 줄로 나눈다 (줄바꿈 문자는 제대로 그린다)."""
    return "\n".join(textwrap.fill(line, width, break_long_words=False, break_on_hyphens=False) for line in text.splitlines())


def suggestion_lines(suggestions: Dict[str, object]) -> str:
    return "\n".join(SUGGESTION_TEXT.get((k, v), f"{k}: {v}") for k, v in suggestions.items())


class DiagnosisDialog(QDialog):
    def __init__(self, server_name: str, diag: remote.Diagnosis, parent: Optional[QWidget] = None,
                 on_apply: Optional[Callable[[Dict[str, object]], None]] = None):
        super().__init__(parent)
        self.diag = diag
        self.on_apply = on_apply
        self.setWindowTitle(f"연결 시험 - {server_name}")
        self.resize(820, 460)
        self.setWindowModality(Qt.WindowModality.NonModal)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(10)
        head = QHBoxLayout()
        head.addWidget(label(f"{server_name}  ·  {diag.address}", "sectionTitle"), 1)
        fails = sum(c.state == "fail" for c in diag.checks)
        warns = sum(c.state == "warn" for c in diag.checks)
        text, tone = (f"실패 {fails}개", "danger") if fails else (f"확인할 것 {warns}개", "warning") if warns else ("모두 정상", "success")
        self.state = Chip(text, tone)
        head.addWidget(self.state)
        layout.addLayout(head)

        self.tree = QTreeWidget()
        self.tree.setObjectName("boxed")
        self.tree.setHeaderLabels(["항목", "결과", "확인한 것"])
        self.tree.setRootIsDecorated(True)
        self.tree.setWordWrap(True)                                      # 긴 설명·고칠 방법은 줄바꿈 (잘라내지 않음)
        self.tree.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        for c in diag.checks:
            mark, tone = MARK[c.state]
            item = QTreeWidgetItem([c.name, mark, _wrap(c.text)])
            item.setForeground(1, theme.tone_color(tone))
            if c.fix:
                fix = QTreeWidgetItem(["", "", _wrap("→ " + c.fix)])
                fix.setForeground(2, theme.tone_color(tone))
                item.addChild(fix)
            self.tree.addTopLevelItem(item)
            item.setExpanded(True)
        self.tree.resizeColumnToContents(0)
        self.tree.resizeColumnToContents(1)
        layout.addWidget(self.tree, 1)

        foot = QHBoxLayout()
        self.suggestion_label = label("", "muted")
        foot.addWidget(self.suggestion_label, 1)
        self.apply_btn = button("추천 설정 적용", "primary")
        self.apply_btn.clicked.connect(self._apply)
        close = button("닫기")
        close.clicked.connect(self.close)
        foot.addWidget(self.apply_btn)
        foot.addWidget(close)
        layout.addLayout(foot)
        if diag.suggestions and on_apply is not None:
            self.suggestion_label.setText("서버 연결 설정을 이렇게 바꾸면 됩니다:  " + suggestion_lines(diag.suggestions).replace("\n", "  ·  "))
            self.apply_btn.setToolTip("서버 연결 폼에 위 값을 넣습니다. [저장]을 눌러야 반영됩니다.")
        else:
            self.suggestion_label.setText("" if diag.ok else "설정으로 고칠 수 있는 것은 없습니다. 서버 쪽을 확인하세요.")
            self.apply_btn.hide()

    def _apply(self) -> None:
        if self.on_apply is not None:
            self.on_apply(dict(self.diag.suggestions))
        self.close()
