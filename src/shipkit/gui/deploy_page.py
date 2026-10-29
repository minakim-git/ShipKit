"""배포 화면: ① 고르기 -> ② 확인 -> ③ 완료. 단계마다 화면 전체를 쓴다.

맨 위의 시작 전 점검 줄은 패키지를 만들기 전에 로컬 저장소·빌드·토큰 문제를 알려준다.
"""
from __future__ import annotations

import re
from typing import Callable, Dict, List, Optional, Tuple

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QButtonGroup, QCheckBox, QComboBox, QFrame, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QStackedWidget, QTableWidget,
    QTableWidgetItem, QTabWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from shipkit import packager, preflight, report, workflow
from shipkit.config import ProjectConfig
from shipkit.gui import theme
from shipkit.gui.context import AppContext
from shipkit.gui.remote_panel import RemotePanel
from shipkit.gui.report_view import show_report
from shipkit.gui.widgets import Card, Chip, CommandRow, StatusDelegate, Stepper, button, label, open_path
from shipkit.models import FileChange, SourceInfo, Status
from shipkit.sources import git_local, providers

STEP_PICK, STEP_REVIEW, STEP_DONE = range(3)
STEP_NAMES = ("고르기", "확인", "완료")
TAB_REQUESTS, TAB_GIT, TAB_FILES, TAB_BUILD = range(4)
STATES = [("머지됨", "merged"), ("열림", "opened"), ("닫힘", "closed"), ("전체", "all")]
STATUS_TEXT = {Status.OK: "성공", Status.WARN: "경고", Status.ERROR: "실패", Status.SKIPPED: "제외"}
STATUS_TONE = {Status.OK: "success", Status.WARN: "warning", Status.ERROR: "danger", Status.SKIPPED: "neutral"}
FILTERS = [("all", "전체", None), ("error", "실패", Status.ERROR), ("warn", "경고", Status.WARN),
           ("ok", "성공", Status.OK), ("skipped", "제외", Status.SKIPPED)]
SERVICE_LABEL = {"gitlab": "GitLab MR", "github": "GitHub PR"}
EXPAND_LIMIT = 300  # 줄 수가 이보다 많으면 처음에 접어 둔다


def _table(headers: List[str]) -> QTableWidget:
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    t.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
    t.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    t.setShowGrid(False)
    t.verticalHeader().setVisible(False)
    t.verticalHeader().setDefaultSectionSize(38)
    t.setWordWrap(False)
    t.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    t.horizontalHeader().setHighlightSections(False)
    return t


def _bar() -> QHBoxLayout:
    row = QHBoxLayout()
    row.setSpacing(12)
    return row


class DeployPage(QWidget):
    def __init__(self, ctx: AppContext):
        super().__init__()
        self.ctx = ctx
        # (프로젝트, 변경 목록, 출처 정보, 미리보기 결과, 배포 계획)
        self.prepared: Optional[Tuple[ProjectConfig, List[FileChange], SourceInfo, packager.PackageResult, list]] = None
        self.last_result: Optional[packager.PackageResult] = None
        self.done_commands: List[str] = []
        self.last_checks: List[preflight.Check] = []
        self._check_token = 0
        self._expanded = True
        self._build_ui()
        ctx.project_listeners.append(self._on_project_changed)
        ctx.connection_listeners.append(lambda: self.project is not None and self.run_preflight())
        theme.on_change(self._repaint_preview)
        ctx.runner.busy_changed.connect(self._set_busy)

    def _repaint_preview(self) -> None:
        """테마가 바뀌면 미리 칠해 둔 미리보기 글자색을 새 색으로 다시 칠한다."""
        if self.prepared is not None and self.last_result is None:
            project, changes, source, result, entries = self.prepared
            self._show_preview((project, changes, source, entries, result))

    @property
    def project(self) -> Optional[ProjectConfig]:
        return self.ctx.project

    # ------------------------------------------------------------------ 화면 구성
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(12)

        head = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(2)
        titles.addWidget(label("배포", "pageTitle"))
        self.mode_label = label("", "pageSubtitle", wrap=False)
        titles.addWidget(self.mode_label)
        head.addLayout(titles, 1)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.progress.setFixedWidth(120)
        self.progress.setVisible(False)
        head.addWidget(self.progress, 0, Qt.AlignmentFlag.AlignVCenter)
        head.addSpacing(12)
        self.stepper = Stepper(STEP_NAMES)
        self.stepper.clicked.connect(self.go_step)
        head.addWidget(self.stepper, 0, Qt.AlignmentFlag.AlignVCenter)
        root.addLayout(head)

        root.addWidget(self._preflight_bar())

        self.steps = QStackedWidget()
        self.steps.addWidget(self._pick_step())
        self.steps.addWidget(self._review_step())
        self.steps.addWidget(self._done_step())
        root.addWidget(self.steps, 1)
        self.empty_label = label("", "muted")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self.empty_label, 1)

    def _preflight_bar(self) -> QWidget:
        self.preflight = QFrame()
        self.preflight.setObjectName("preflight")
        row = QHBoxLayout(self.preflight)
        row.setContentsMargins(14, 5, 6, 5)
        row.setSpacing(6)
        row.addWidget(label("시작 전 점검", "fieldLabel", wrap=False))
        row.addSpacing(6)
        self.check_row = QHBoxLayout()
        self.check_row.setSpacing(6)
        row.addLayout(self.check_row)
        row.addStretch(1)
        # '원격보다 N커밋 뒤처짐' 배지 바로 옆에 붙는 작은 버튼 (_show_checks 에서 자리를 잡는다)
        self.pull_btn = button("pull 받기", "chipAction")
        theme.bind_icon(self.pull_btn, "download.svg", "warning", 14)
        self.pull_btn.setToolTip("원격의 최신 커밋을 받습니다 (git pull --ff-only). 합치기(merge)는 하지 않습니다.")
        self.pull_btn.clicked.connect(self.pull)
        self.pull_btn.hide()
        self.recheck_btn = button("다시 점검", "ghost")
        theme.bind_icon(self.recheck_btn, "refresh.svg", "PRIMARY", 16)
        self.recheck_btn.clicked.connect(self.run_preflight)
        row.addWidget(self.recheck_btn)
        self.check_chips: List[Chip] = []
        return self.preflight

    # ---- ① 고르기
    def _pick_step(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        card = Card(padding=14)
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.addTab(self._requests_tab(), "MR / PR")
        self.tabs.addTab(self._git_tab(), "git 범위")
        self.tabs.addTab(self._files_tab(), "파일 목록")
        self.tabs.addTab(self._build_tab(), "빌드 결과 전체")
        self.tabs.currentChanged.connect(self._invalidate)
        self.tabs.currentChanged.connect(lambda i: self.req_hint.setVisible(i == TAB_REQUESTS))
        self.tabs.setCornerWidget(self.req_hint, Qt.Corner.TopRightCorner)
        card.body.addWidget(self.tabs, 1)
        layout.addWidget(card, 1)

        bar = _bar()
        self.pick_summary = label("", "muted", wrap=False)
        self.preview_btn = button("다음: 미리보기", "primary")
        self.preview_btn.setIcon(theme.svg_icon("arrow-right.svg", "#FFFFFF", 16))
        self.preview_btn.setLayoutDirection(Qt.LayoutDirection.RightToLeft)  # 화살표를 글자 뒤에
        self.preview_btn.setMinimumWidth(150)
        self.preview_btn.clicked.connect(self.preview)
        bar.addWidget(self.pick_summary, 1)
        bar.addWidget(self.preview_btn)
        layout.addLayout(bar)
        return page

    def _requests_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(10)
        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.service_combo = QComboBox()
        self.service_combo.currentIndexChanged.connect(self._invalidate)
        self.state_combo = QComboBox()
        for text, value in STATES:
            self.state_combo.addItem(text, value)
        self.state_combo.setFixedWidth(110)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("제목으로 검색")
        self.search_edit.returnPressed.connect(self.load_requests)
        self.load_btn = button("목록 불러오기")
        self.load_btn.clicked.connect(self.load_requests)
        self.extra_edit = QLineEdit()
        self.extra_edit.setPlaceholderText("목록에 없는 번호 (예: 612 615)")
        self.extra_edit.setFixedWidth(230)
        self.extra_edit.textChanged.connect(self._invalidate)
        bar.addWidget(self.service_combo)
        bar.addWidget(self.state_combo)
        bar.addWidget(self.search_edit, 1)
        bar.addWidget(self.load_btn)
        bar.addSpacing(8)
        bar.addWidget(self.extra_edit)
        layout.addLayout(bar)
        self.req_table = _table(["", "번호", "제목", "작성자", "머지 / 수정 시각"])
        self.req_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        for col, width in ((0, 44), (1, 76), (3, 96), (4, 150)):  # 목록을 불러오기 전에도 제목이 잘리지 않게
            self.req_table.setColumnWidth(col, width)
        self.req_table.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.req_table.cellClicked.connect(self._toggle_row)
        self.req_table.itemChanged.connect(self._invalidate)
        layout.addWidget(self.req_table, 1)
        self.req_hint = label("", "muted", wrap=False)  # 탭 줄 오른쪽에 붙는다
        self.req_hint.setContentsMargins(0, 0, 4, 0)
        return w

    def _git_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(8)
        row = QHBoxLayout()
        row.addWidget(label("범위", "fieldLabel", wrap=False))
        self.range_edit = QLineEdit()
        self.range_edit.setPlaceholderText("예: v1.2..HEAD, abc123..def456")
        self.range_edit.textChanged.connect(self._invalidate)
        row.addWidget(self.range_edit, 1)
        layout.addLayout(row)
        layout.addWidget(label("두 커밋(또는 태그) 사이에 바뀐 파일을 배포합니다. 기본값은 가장 최근 태그부터 지금(HEAD)까지입니다.",
                               "muted"))
        layout.addStretch(1)
        return w

    def _files_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(0, 4, 0, 0)
        self.files_edit = QPlainTextEdit()
        self.files_edit.setPlaceholderText(
            "한 줄에 경로 하나씩 붙여넣으세요.\n"
            "src/main/webapp/WEB-INF/jsp/a.jsp\n"
            "git diff --name-status 결과도 됩니다 (D<TAB>경로 = 삭제)")
        self.files_edit.textChanged.connect(self._invalidate)
        layout.addWidget(self.files_edit, 1)
        return w

    def _build_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(0, 4, 0, 0)
        self.build_hint = label("", "muted")
        layout.addWidget(self.build_hint)
        layout.addStretch(1)
        return w

    # ---- ② 확인
    def _review_step(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        card = Card(padding=16)
        self.summary_label = label("", "muted")
        card.body.addWidget(self.summary_label)
        self.messages = QListWidget()
        self.messages.setObjectName("messages")
        # 알림은 읽기만 하는 목록: 고르면 글자색이 선택색으로 바뀌어 안 보이므로 선택을 막는다
        self.messages.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.messages.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.messages.setWordWrap(True)
        self.messages.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        card.body.addWidget(self.messages)

        filters = QHBoxLayout()
        filters.setSpacing(6)
        self.filter_group = QButtonGroup(self)
        self.filter_buttons: Dict[str, QPushButton] = {}
        for key, text, _status in FILTERS:
            b = QPushButton(text)
            b.setObjectName("seg")
            b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            self.filter_group.addButton(b)
            self.filter_buttons[key] = b
            filters.addWidget(b)
            b.clicked.connect(self._apply_filter)
        self.filter_buttons["all"].setChecked(True)
        filters.addStretch(1)
        self.expand_btn = button("모두 접기", "ghost")
        self.expand_btn.clicked.connect(lambda: self._set_expanded(not self._expanded))
        filters.addWidget(self.expand_btn)
        card.body.addLayout(filters)

        self.result_tree = QTreeWidget()
        self.result_tree.setColumnCount(3)
        self.result_tree.setHeaderLabels(["상태", "구분", "소스 파일  →  서버에 반영될 파일"])
        self.result_tree.setItemDelegateForColumn(0, StatusDelegate(self.result_tree))
        self.result_tree.setUniformRowHeights(True)
        self.result_tree.setIndentation(16)
        self.result_tree.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.result_tree.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.result_tree.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        header = self.result_tree.header()
        header.setStretchLastSection(True)
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        header.setSectionsMovable(False)
        self.result_tree.setColumnWidth(0, 100)
        self.result_tree.setColumnWidth(1, 60)
        card.body.addWidget(self.result_tree, 1)
        layout.addWidget(card, 1)

        bar = _bar()
        self.back_btn = button("이전")
        self.back_btn.clicked.connect(lambda: self.go_step(STEP_PICK))
        self.archive_check = QCheckBox("압축 파일도 만들기")
        self.strict_check = QCheckBox("경고도 실패로 처리")
        self.make_btn = button("패키지 만들기", "primary")
        self.make_btn.setMinimumWidth(150)
        self.make_btn.setEnabled(False)
        self.make_btn.clicked.connect(self.make_package)
        bar.addWidget(self.back_btn)
        bar.addSpacing(8)
        bar.addWidget(self.archive_check)
        bar.addWidget(self.strict_check)
        bar.addStretch(1)
        bar.addWidget(self.make_btn)
        layout.addLayout(bar)
        return page

    # ---- ③ 완료
    def _done_step(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        card = Card(padding=22)
        top = QHBoxLayout()
        top.setSpacing(16)
        icon = QLabel()
        icon.setPixmap(theme.svg_icon("success.svg", "", 48).pixmap(48, 48))
        theme.on_change(lambda: icon.setPixmap(theme.svg_icon("success.svg", "", 48).pixmap(48, 48)))
        top.addWidget(icon, 0, Qt.AlignmentFlag.AlignTop)
        texts = QVBoxLayout()
        texts.setSpacing(6)
        texts.addWidget(label("패키지를 만들었습니다", "doneTitle"))
        self.done_label = label("", "code")
        self.done_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        texts.addWidget(self.done_label)
        self.done_chips = QHBoxLayout()
        self.done_chips.setSpacing(6)
        texts.addLayout(self.done_chips)
        top.addLayout(texts, 1)
        card.body.addLayout(top)

        # 서버에 바로 올리고 반영 (서버 연결을 등록한 프로젝트)
        self.remote_panel = RemotePanel(self.ctx)

        manual = Card("직접 반영할 때 (서버에서 실행할 명령)")
        self.upload_label = label("", "muted")
        manual.body.addWidget(self.upload_label)
        self.command_box = QVBoxLayout()
        self.command_box.setSpacing(6)
        manual.body.addLayout(self.command_box)
        self.hooks_label = label("", "muted")
        manual.body.addWidget(self.hooks_label)

        content = QWidget()
        stack = QVBoxLayout(content)
        stack.setContentsMargins(0, 0, 6, 0)
        stack.setSpacing(12)
        stack.addWidget(card)            # 결과
        stack.addWidget(self.remote_panel)  # 바로 반영
        stack.addWidget(manual)          # 직접 반영
        stack.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(content)
        scroll.viewport().setAutoFillBackground(False)
        content.setAutoFillBackground(False)
        layout.addWidget(scroll, 1)

        bar = _bar()
        self.open_folder_btn = button("폴더 열기")
        self.open_report_btn = button("보고서 열기")
        self.open_folder_btn.clicked.connect(lambda: self.last_result and open_path(self.last_result.out_dir))
        self.open_report_btn.clicked.connect(
            lambda: self.last_result and show_report(self, self.last_result.out_dir / "report.md"))
        self.new_btn = button("새 배포", "primary")
        self.new_btn.setMinimumWidth(120)
        self.new_btn.clicked.connect(self.start_over)
        bar.addWidget(self.open_folder_btn)
        bar.addWidget(self.open_report_btn)
        bar.addStretch(1)
        bar.addWidget(self.new_btn)
        layout.addLayout(bar)
        return page

    # ------------------------------------------------------------------ 단계 이동
    @property
    def step(self) -> int:
        return self.steps.currentIndex()

    def _reachable(self) -> int:
        if self.last_result is not None:
            return STEP_DONE
        return STEP_REVIEW if self.prepared is not None else STEP_PICK

    def go_step(self, step: int) -> None:
        self.steps.setCurrentIndex(step)
        self.preflight.setVisible(step != STEP_DONE and self.project is not None)
        self.stepper.set_state(step, self._reachable())

    def start_over(self) -> None:
        """완료 화면의 [새 배포]: 고른 MR·입력은 비우고 처음 단계로."""
        self.req_table.blockSignals(True)
        for row in range(self.req_table.rowCount()):
            self.req_table.item(row, 0).setCheckState(Qt.CheckState.Unchecked)
        self.req_table.blockSignals(False)
        self.extra_edit.clear()
        self.files_edit.clear()
        self.last_result = None
        self._invalidate()
        self.go_step(STEP_PICK)
        self.run_preflight()

    def prefill(self, record) -> None:
        """기록에서 [같은 내용으로 다시 만들기]: 같은 MR/PR 번호나 같은 파일 목록을 ① 고르기에 채운다.
        파일은 지금 로컬 저장소에서 다시 가져오므로, 그 사이 바뀐 내용이 있으면 새 내용이 들어간다."""
        self.start_over()
        kind = record.request_kind
        if record.requests and kind and self.tabs.isTabVisible(TAB_REQUESTS):
            self.service_combo.setCurrentIndex(max(0, self.service_combo.findData(kind)))
            self.tabs.setCurrentIndex(TAB_REQUESTS)
            self.extra_edit.setText(" ".join(str(r["iid"]) for r in record.requests))
        elif record.source_kind == "build" and self.tabs.isTabVisible(TAB_BUILD):
            self.tabs.setCurrentIndex(TAB_BUILD)
        else:
            self.tabs.setCurrentIndex(TAB_FILES)
            self.files_edit.setPlainText("\n".join(record.changes))
        self._update_pick_summary()

    # ------------------------------------------------------------------ 프로젝트
    def _on_project_changed(self) -> None:
        cfg, p = self.ctx.cfg, self.project
        self.prepared, self.last_result = None, None
        self.req_table.setRowCount(0)
        has = p is not None
        self.steps.setVisible(has)
        self.stepper.setVisible(has)
        self.empty_label.setVisible(not has)
        if not has:
            self.mode_label.setText("")
            self.empty_label.setText(f"설정 파일을 읽지 못했습니다.\n{self.ctx.load_error}" if self.ctx.load_error else
                                     "왼쪽 위 프로젝트 목록에서 프로젝트를 추가하세요.")
            self.go_step(STEP_PICK)
            return
        self.mode_label.setText(f"{p.describe_mode}  ·  서버 경로 {p.server_path}")
        self.archive_check.setText("압축 파일도 만들기 (.zip)" if p.server_os == "windows" else "압축 파일도 만들기 (.tar.gz)")

        kinds = providers.available(cfg, p)
        self.service_combo.blockSignals(True)
        self.service_combo.clear()
        for kind in kinds:
            self.service_combo.addItem(SERVICE_LABEL[kind], kind)
        self.service_combo.blockSignals(False)
        self.service_combo.setVisible(len(kinds) > 1)
        is_git = p.root.is_dir() and git_local.is_git_repo(p.root)
        self.tabs.setTabVisible(TAB_REQUESTS, bool(kinds))
        if kinds:
            self.tabs.setTabText(TAB_REQUESTS, " / ".join(SERVICE_LABEL[k] for k in kinds))
            self.req_hint.setText("[목록 불러오기]를 누르세요. 여러 개를 고르면 머지된 순서대로 합칩니다.")
        self.tabs.setTabVisible(TAB_GIT, is_git)
        self.tabs.setTabVisible(TAB_BUILD, p.mode == "sync")
        self.build_hint.setText(f"{p.build_dir} 폴더 전체를 패키지에 담고, 서버에서는 내용이 다른 파일만 반영합니다. "
                                "이번 빌드에 없는 옛 파일은 정리 폴더에서 지워집니다." if p.mode == "sync" else "")
        self.range_edit.setText(workflow.latest_tag_range(p) if is_git else "")
        default = TAB_BUILD if p.mode == "sync" else (TAB_REQUESTS if kinds else TAB_FILES)
        self.tabs.setCurrentIndex(default)
        self.req_hint.setVisible(default == TAB_REQUESTS)
        self._invalidate()
        self.go_step(STEP_PICK)
        self.run_preflight()

    def run_preflight(self) -> None:
        cfg, p = self.ctx.cfg, self.project
        if p is None:
            return
        self._check_token += 1
        token = self._check_token
        self._show_checks([preflight.Check("neutral", "점검 중…")])

        def done(checks):
            if token == self._check_token:  # 그 사이 프로젝트를 바꿨으면 버린다
                self._show_checks(checks)

        def failed(msg):
            if token == self._check_token:
                self._show_checks([preflight.Check("warning", "점검 실패", msg)])

        self.ctx.runner.run(lambda: preflight.run(cfg, p), done, failed, quiet=True)

    def _show_checks(self, checks: List[preflight.Check]) -> None:
        self.check_row.removeWidget(self.pull_btn)
        self.pull_btn.hide()
        for chip in self.check_chips:
            self.check_row.removeWidget(chip)
            chip.hide()  # 지우기는 다음 이벤트 때 일어나므로 그 전에 가린다
            chip.deleteLater()
        self.check_chips = []
        for c in checks:
            chip = Chip(c.text, c.tone)
            chip.setToolTip(c.hint)
            self.check_row.addWidget(chip, 0, Qt.AlignmentFlag.AlignVCenter)
            self.check_chips.append(chip)
            if c.action == preflight.ACTION_PULL:
                self.check_row.addWidget(self.pull_btn, 0, Qt.AlignmentFlag.AlignVCenter)
                self.pull_btn.show()
        self.last_checks = checks

    def pull(self) -> None:
        """점검 띠의 [pull 받기]: 원격 최신 커밋을 fast-forward 로만 받는다."""
        p = self.project
        if p is None:
            return
        dirty = next((c for c in self.last_checks if c.text.startswith("커밋 안 된 수정")), None)
        if dirty and not self.ctx.dialogs.ask(
                "pull 받기", f"{dirty.text}가 있습니다.\n받을 파일과 겹치지 않으면 그대로 두고 받고, "
                             "겹치면 아무것도 바꾸지 않고 멈춥니다.\n\n받을까요?"):
            return
        self.pull_btn.setEnabled(False)
        self.pull_btn.setText("받는 중…")

        def finish() -> None:
            self.pull_btn.setEnabled(True)
            self.pull_btn.setText("pull 받기")
            self.run_preflight()

        def done(count: int) -> None:
            finish()
            if not count:
                self.ctx.dialogs.info("pull 받기", "이미 최신입니다.")
                return
            needs_build = p.mode == "sync" or any(r.build_dir for r in p.rules)
            text = f"원격에서 커밋 {count}개를 받았습니다."
            if needs_build:
                text += "\n빌드가 필요한 프로젝트입니다. 다시 빌드(컴파일)한 뒤 미리보기를 하세요."
            if self.prepared is not None:  # 받기 전에 만든 미리보기는 옛 내용이므로 다시 하게 한다
                self._invalidate()
                self.go_step(STEP_PICK)
                text += "\n받기 전에 만든 미리보기는 지웠습니다."
            self.ctx.dialogs.info("pull 받기", text)

        def failed(msg: str) -> None:
            finish()
            self.ctx.dialogs.error("pull 받지 못했습니다", msg)

        self.ctx.runner.run(lambda: git_local.pull_ff(p.root), done, failed)

    # ------------------------------------------------------------------ 상태
    def _invalidate(self, *_args) -> None:
        self.prepared = None
        self.make_btn.setEnabled(False)
        self._update_pick_summary()
        self.stepper.set_state(self.step, self._reachable())

    def _update_pick_summary(self) -> None:
        tab = self.tabs.currentIndex()
        if tab == TAB_REQUESTS:
            prefix = "#" if self.service_combo.currentData() == "github" else "!"
            numbers = self.selected_numbers()
            text = (f"{len(numbers)}개 선택됨  ·  " + ", ".join(f"{prefix}{n}" for n in numbers)) if numbers else \
                "배포할 MR/PR 을 고르세요."
        elif tab == TAB_GIT:
            text = f"{self.range_edit.text().strip() or '-'}  사이에 바뀐 파일"
        elif tab == TAB_FILES:
            count = sum(1 for line in self.files_edit.toPlainText().splitlines() if line.strip())
            text = f"파일 {count}개" if count else "배포할 파일 경로를 붙여넣으세요."
        else:
            text = "빌드 결과 전체를 담습니다."
        self.pick_summary.setText(text)

    def _set_busy(self, busy: bool) -> None:
        self.progress.setVisible(busy)
        for w in (self.preview_btn, self.load_btn, self.back_btn, self.new_btn):
            w.setEnabled(not busy)
        if busy:
            self.make_btn.setEnabled(False)
            for b in self.stepper.buttons:
                b.setEnabled(False)
        else:
            self.make_btn.setEnabled(self.prepared is not None and self._has_output())
            self.stepper.set_state(self.step, self._reachable())

    def _has_output(self) -> bool:
        result = self.prepared[3] if self.prepared else None
        return bool(result and (result.manifest or result.deletes))

    # ------------------------------------------------------------------ MR/PR 목록
    def load_requests(self) -> None:
        if not self.project or self.service_combo.count() == 0:
            return
        kind = self.service_combo.currentData()
        state = self.state_combo.currentData()
        search = self.search_edit.text().strip() or None
        cfg, project = self.ctx.cfg, self.project

        def fetch():
            provider = providers.make_provider(cfg, project, kind)
            return provider, provider.list(state=state, limit=50, search=search)

        self.ctx.runner.run(fetch, self._show_requests, lambda msg: self.ctx.dialogs.error("목록 불러오기 실패", msg))

    def _show_requests(self, payload) -> None:
        provider, items = payload
        self.req_table.blockSignals(True)
        self.req_table.setRowCount(0)
        for it in items:
            row = self.req_table.rowCount()
            self.req_table.insertRow(row)
            check = QTableWidgetItem()
            check.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            check.setCheckState(Qt.CheckState.Unchecked)
            check.setData(Qt.ItemDataRole.UserRole, it["iid"])
            when = (it.get("merged_at") or it.get("updated_at") or "")[:16].replace("T", " ")
            values = [f"{provider.prefix}{it['iid']}", it.get("title", ""), (it.get("author") or {}).get("name", ""), when]
            self.req_table.setItem(row, 0, check)
            for col, value in enumerate(values, 1):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                if col == 1:
                    item.setForeground(QColor(theme.PRIMARY))
                self.req_table.setItem(row, col, item)
        self.req_table.blockSignals(False)
        header = self.req_table.horizontalHeader()
        for col in (1, 3, 4):
            self.req_table.resizeColumnToContents(col)
            self.req_table.setColumnWidth(col, max(self.req_table.columnWidth(col), header.sectionSizeHint(col)))
        self.req_table.setColumnWidth(0, 44)
        self.req_hint.setText("조회된 항목이 없습니다. 상태(머지됨/열림)나 검색어를 바꿔 보세요." if not items else
                              f"{len(items)}개 · 행을 누르면 선택됩니다. 여러 개를 고르면 머지된 순서대로 합칩니다.")
        self._invalidate()

    def _toggle_row(self, row: int, col: int) -> None:
        if col == 0:  # 체크 칸은 Qt 가 직접 토글한다
            return
        item = self.req_table.item(row, 0)
        checked = item.checkState() == Qt.CheckState.Checked
        item.setCheckState(Qt.CheckState.Unchecked if checked else Qt.CheckState.Checked)

    def selected_numbers(self) -> List[int]:
        numbers = []
        for row in range(self.req_table.rowCount()):
            item = self.req_table.item(row, 0)
            if item and item.checkState() == Qt.CheckState.Checked:
                numbers.append(int(item.data(Qt.ItemDataRole.UserRole)))
        numbers += [int(n) for n in re.findall(r"\d+", self.extra_edit.text())]
        return list(dict.fromkeys(numbers))

    # ------------------------------------------------------------------ 미리보기 / 만들기
    def _collector(self) -> Optional[Callable[[], Tuple[List[FileChange], SourceInfo]]]:
        """현재 탭의 입력으로 변경 수집 함수를 만든다. 입력이 부족하면 안내하고 None."""
        cfg, project, tab = self.ctx.cfg, self.project, self.tabs.currentIndex()
        if tab == TAB_REQUESTS:
            numbers = self.selected_numbers()
            if not numbers:
                self.ctx.dialogs.error("입력 확인", "배포할 MR/PR 을 하나 이상 고르세요.")
                return None
            kind = self.service_combo.currentData()
            return lambda: workflow.collect_requests(providers.make_provider(cfg, project, kind), project, numbers)
        if tab == TAB_GIT:
            rng = self.range_edit.text().strip()
            if not rng:
                self.ctx.dialogs.error("입력 확인", "git 범위를 입력하세요. 예: v1.2..HEAD")
                return None
            return lambda: workflow.collect_git(project, rng)
        if tab == TAB_FILES:
            lines = [line for line in self.files_edit.toPlainText().splitlines() if line.strip()]
            if not lines:
                self.ctx.dialogs.error("입력 확인", "배포할 파일 경로를 붙여넣으세요.")
                return None
            return lambda: workflow.collect_lines(lines, "직접 입력 (붙여넣기)")
        return lambda: workflow.collect_build(project)

    def preview(self) -> None:
        if not self.project:
            return
        collect = self._collector()
        if collect is None:
            return
        project = self.project

        def run():
            changes, source = collect()
            entries = workflow.plan(project, changes, source)
            result = packager.build_package(project, entries, source, dry_run=True, source_changes=changes)
            return project, changes, source, entries, result

        self.ctx.runner.run(run, self._show_preview, lambda msg: self.ctx.dialogs.error("미리보기 실패", msg))

    def _show_preview(self, payload) -> None:
        project, changes, source, entries, result = payload
        self.prepared = (project, changes, source, result, entries)
        self.last_result = None
        self.messages.clear()
        for text in source.warnings:
            self._message(f"⚠  {text}", "warning")
        for text in source.notes:
            self._message(f"ℹ  {text}", "info")
        for stage, commands in result.hooks.items():
            for cmd in commands:
                self._message(f"▶  서버에서 실행할 명령 ({stage}): {cmd}", "neutral")
        self.messages.setVisible(self.messages.count() > 0)
        self._fit_messages()

        rows = entries
        if project.mode == "sync":  # 빌드 결과는 수천 개일 수 있어 확인할 항목만 보여 준다
            rows = [e for e in entries if e.status in (Status.ERROR, Status.WARN)]
        order = {Status.ERROR: 0, Status.WARN: 1, Status.OK: 2, Status.SKIPPED: 3}
        rows = sorted(rows, key=lambda e: (order[e.status], e.change.path))  # 확인할 항목을 위로
        self._fill_tree(rows)

        counts = {s: result.count(s) for s in Status}
        head = f"{source.description}  ·  "
        if project.mode == "sync":
            head += f"빌드 결과 {len(result.manifest)}개 파일 (서버와 다른 파일만 반영)"
        else:
            head += f"서버 반영 {len(result.manifest)}개 · 서버 삭제 {len(result.deletes)}개"
        head += (f"  ·  성공 {counts[Status.OK]} / 경고 {counts[Status.WARN]} / 실패 {counts[Status.ERROR]}"
                 f" / 제외 {counts[Status.SKIPPED]}")
        if not (result.manifest or result.deletes):
            head += "  -  서버에 반영할 파일이 없습니다."
        self.summary_label.setText(head)

        shown = {s: sum(1 for e in rows if e.status == s) for s in Status}
        for key, text, status in FILTERS:
            n = len(rows) if status is None else shown[status]
            b = self.filter_buttons[key]
            b.setText(f"{text}  {n}")
            b.setVisible(status is None or n > 0)
        self.filter_buttons["all"].setChecked(True)
        self._apply_filter()
        self.go_step(STEP_REVIEW)
        self.make_btn.setEnabled(bool(result.manifest or result.deletes))

    def _fill_tree(self, rows) -> None:
        tree = self.result_tree
        tree.clear()
        muted, total = QColor(theme.MUTED), 0
        for e in rows:
            top = QTreeWidgetItem([STATUS_TEXT[e.status], e.change.change.label, e.change.path])
            top.setData(0, Qt.ItemDataRole.UserRole, STATUS_TONE[e.status])
            top.setData(2, Qt.ItemDataRole.UserRole, e.status)
            top.setToolTip(2, e.change.path)
            if e.status == Status.SKIPPED:
                top.setForeground(2, QColor(theme.SUBTLE))
            for target in [a.target for a in e.artifacts] + [f"(삭제) {d}" for d in e.deletes]:
                child = QTreeWidgetItem(["", "", f"→  {target}"])
                child.setForeground(2, muted)
                child.setToolTip(2, target)
                top.addChild(child)
            for msg in e.messages:
                tone = {Status.ERROR: "danger", Status.WARN: "warning"}.get(e.status, "neutral")
                child = QTreeWidgetItem(["", "", f"{'ℹ' if tone == 'neutral' else '⚠'}  {msg}"])
                child.setForeground(2, QColor(theme.TONES[tone][0]))
                child.setToolTip(2, msg)
                top.addChild(child)
            tree.addTopLevelItem(top)
            total += 1 + top.childCount()
        self._set_expanded(total <= EXPAND_LIMIT)

    def _set_expanded(self, on: bool) -> None:
        self._expanded = on
        if on:
            self.result_tree.expandAll()
        else:
            self.result_tree.collapseAll()
        self.expand_btn.setText("모두 접기" if on else "모두 펼치기")

    def _apply_filter(self) -> None:
        wanted = next(status for key, _text, status in FILTERS if self.filter_buttons[key].isChecked())
        for i in range(self.result_tree.topLevelItemCount()):
            item = self.result_tree.topLevelItem(i)
            item.setHidden(wanted is not None and item.data(2, Qt.ItemDataRole.UserRole) != wanted)

    def _fit_messages(self) -> None:
        """긴 안내는 줄바꿈해서 다 보이게 하고, 목록 높이는 내용만큼 (5줄 넘으면 스크롤)."""
        width = max(self.messages.viewport().width(), 400)
        metrics = self.messages.fontMetrics()
        total = 4
        for i in range(min(self.messages.count(), 5)):
            text = self.messages.item(i).text()
            rect = metrics.boundingRect(0, 0, width - 24, 10000, Qt.TextFlag.TextWordWrap, text)
            height = rect.height() + 14
            self.messages.item(i).setSizeHint(QSize(width - 4, height))
            total += height
        self.messages.setFixedHeight(total)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self.messages.count():
            self._fit_messages()

    def _message(self, text: str, tone: str) -> None:
        fg, bg = theme.TONES[tone]
        item = QListWidgetItem(text)
        item.setForeground(QColor(fg))
        item.setBackground(QColor(bg))
        item.setToolTip(text)
        self.messages.addItem(item)

    def make_package(self) -> None:
        if not self.prepared:
            return
        project, changes, source, preview, entries = self.prepared
        strict = self.strict_check.isChecked()
        if workflow.is_blocked(entries, source, strict):
            failed = preview.count(Status.ERROR)
            question = (f"실패 항목 {failed}개가 있습니다. 실패 항목을 빼고 나머지로 패키지를 만들까요?" if failed
                        else "경고가 있습니다('경고도 실패로 처리' 선택됨). 그래도 패키지를 만들까요?")
            if not self.ctx.dialogs.ask("확인", question):
                return
        archive = ("zip" if project.server_os == "windows" else "tar") if self.archive_check.isChecked() else None

        def run():
            return packager.build_package(project, entries, source, archive=archive, source_changes=changes)

        self.ctx.runner.run(run, self._show_created, lambda msg: self.ctx.dialogs.error("패키지 만들기 실패", msg))

    def _show_created(self, result: packager.PackageResult) -> None:
        self.last_result = result
        self.remote_panel.set_package(result)
        self.prepared = None
        self.make_btn.setEnabled(False)
        self.done_label.setText(str(result.out_dir) + (f"\n{result.archive}" if result.archive else ""))
        _clear(self.done_chips)
        facts = [(f"서버 반영 {len(result.manifest)}개", "primary")]
        if result.deletes:
            facts.append((f"서버 삭제 {len(result.deletes)}개", "neutral"))
        if result.count(Status.ERROR):
            facts.append((f"실패 {result.count(Status.ERROR)}개는 빠짐", "danger"))
        if result.archive:
            facts.append(("압축 " + ("zip" if result.archive_kind == "zip" else "tar.gz"), "neutral"))
        for text, tone in facts:
            self.done_chips.addWidget(Chip(text, tone))
        self.done_chips.addStretch(1)

        upload, steps, (rollback, rollback_desc) = report.server_steps(result)
        self.upload_label.setText(f"{upload}  그다음 서버에서 아래 명령을 순서대로 실행합니다.")
        _clear(self.command_box)
        self.done_commands = [cmd for cmd, _ in steps] + [rollback]
        for i, (cmd, desc) in enumerate(steps, 1):
            self.command_box.addWidget(CommandRow(str(i), cmd, desc))
        self.command_box.addWidget(CommandRow("↺", rollback, rollback_desc))
        hooks = [f"{stage}: {cmd}" for stage, cmds in result.hooks.items() for cmd in cmds]
        self.hooks_label.setText("서버 스크립트가 함께 실행하는 명령  ·  " + "  /  ".join(hooks) if hooks else "")
        self.hooks_label.setVisible(bool(hooks))
        self.go_step(STEP_DONE)


def _clear(layout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        if item.widget():
            item.widget().hide()
            item.widget().deleteLater()
