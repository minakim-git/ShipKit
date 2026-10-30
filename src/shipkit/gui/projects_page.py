"""프로젝트 설정 화면: 왼쪽 위에서 고른 프로젝트의 설정을 탭별로 편집한다.

기본 · 반영 규칙(경로 시험) · 서버 명령 · 제외·알림 · 서버 연결(servers_tab). 입력 위젯과 새 프로젝트 창은 project_widgets.
화면에서 고친 값은 개인 설정에 저장한다.
비워 둔 칸은 저장소의 .shipkit.yml(있으면)·프리셋·기본값을 따르고, 그 값을 흐린 글씨로 보여 준다.
"""
from __future__ import annotations

import copy
import posixpath
from typing import Any, Dict, List, Optional, Sequence, Tuple

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QComboBox, QFrame, QHBoxLayout, QLineEdit, QPlainTextEdit, QScrollArea, QStackedWidget, QTabWidget,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from shipkit import CONFIG_NAME, PROJECT_FILE, config_store, legacy, mapper, project_edit, workflow
from shipkit.config import (
    _COMMON_EXCLUDE, DEFAULT_ALLOW, DEFAULT_DENY, DEFAULT_HISTORY_FILE, DEFAULT_PACKAGE_NAME, PRESETS, ConfigError,
    ProjectConfig,
)
from shipkit.gui import theme
from shipkit.gui.context import AppContext
from shipkit.gui.deploy_page import STATUS_TEXT, STATUS_TONE
from shipkit.gui.project_widgets import INHERIT, LineList, OverrideBox, ProjectDialog, RowTable, form_layout
from shipkit.gui.servers_tab import ServersTab
from shipkit.gui.widgets import Card, Chip, PathField, StatusDelegate, button, label, page_header
from shipkit.models import Status
from shipkit.workflow import InputError

TAB_BASIC, TAB_RULES, TAB_HOOKS, TAB_FILTERS, TAB_SERVERS = range(5)
# (단계, 제목, 언제 실행되는지, 빈 표에 보여 줄 예시)
HOOK_STAGES = (("pre_apply", "반영 전", "서버 파일을 바꾸기 전에 실행합니다. 실패하면 파일을 바꾸지 않고 멈춥니다.",
                "예: sudo systemctl stop tomcat   (서비스를 멈춘 뒤 반영해야 할 때)"),
               ("post_apply", "반영 후", "파일을 모두 바꾼 뒤 실행합니다. 가장 많이 쓰는 단계입니다.",
                "예: sudo systemctl restart tomcat"),
               ("post_rollback", "되돌린 후", "rollback 으로 이전 파일로 되돌린 뒤 실행합니다. 보통 '반영 후'와 같은 재시작 명령.",
                "예: sudo systemctl restart tomcat"))
HOOK_EXAMPLE = ("$ ./shipkit.sh apply\n"
                "[정보] 반영 12개, 동일(건너뜀) 3개, 삭제 0개, 정리 0개, 실패 0개\n"
                "== post_apply 명령 ==\n"
                "    sudo systemctl restart tomcat\n"
                "위 명령을 실행할까요? [y/N] y")
MODE_TEXT = {"changes": "변경 파일 매핑", "sync": "빌드 결과 동기화"}


_form = form_layout


def _split(text: str) -> List[str]:
    return [x.strip() for x in text.replace("\n", ",").split(",") if x.strip()]


def _show_prefix(p: str) -> str:
    return p if p else "."


def _server_target(p: ProjectConfig, rel: str) -> str:
    """서버 배포 경로 기준 상대 경로 -> 서버의 전체 경로 (Windows 서버는 역슬래시 구분자)."""
    if p.server_os == "windows":
        return p.server_path.rstrip("\\") + "\\" + rel.replace("/", "\\")
    return posixpath.join(p.server_path, rel)


class ProjectsPage(QWidget):
    def __init__(self, ctx: AppContext):
        super().__init__()
        self.ctx = ctx
        self.dialog_factory = ProjectDialog  # 테스트에서 바꿔 끼운다
        self.data: Dict[str, Any] = {}
        self.layers: Optional[project_edit.Layers] = None
        self.loaded_name: Optional[str] = None
        self.draft: Optional[ProjectConfig] = None
        self.baseline: Dict[str, Any] = {}
        self.dirty = False
        self._loading = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        actions = QWidget()
        bar = QHBoxLayout(actions)
        bar.setContentsMargins(0, 0, 0, 0)
        bar.setSpacing(8)
        self.revert_btn = button("되돌리기")
        self.revert_btn.clicked.connect(self.revert)
        self.save_btn = button("저장", "primary")
        self.save_btn.setMinimumWidth(110)
        self.save_btn.clicked.connect(self.save)
        bar.addWidget(self.revert_btn)
        bar.addWidget(self.save_btn)
        self.header = page_header("프로젝트 설정", "", actions)
        self.subtitle = self.header.findChild(QWidget, "pageSubtitle")
        layout.addWidget(self.header)

        self.body = QStackedWidget()
        layout.addWidget(self.body, 1)
        self.body.addWidget(self._empty_view())
        main = QWidget()
        main_layout = QVBoxLayout(main)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(12)
        main_layout.addWidget(self._status_bar())
        self.tabs = QTabWidget()
        self.tabs.addTab(self._scroll(self._basic_tab()), "기본")
        self.tabs.addTab(self._scroll(self._rules_tab()), "반영 규칙")
        self.tabs.addTab(self._scroll(self._hooks_tab()), "서버 명령")
        self.tabs.addTab(self._scroll(self._filters_tab()), "제외·알림")
        self.servers_tab = ServersTab(
            ctx, project_path=lambda: self.server_edit.text().strip() or self.server_edit.placeholderText().split(": ", 1)[-1],
            server_os=lambda: self.draft.server_os if self.draft else "linux")
        self.servers_tab.changed.connect(self._changed)
        self.tabs.addTab(self._scroll(self.servers_tab), "서버 연결")
        main_layout.addWidget(self.tabs, 1)
        self.body.addWidget(main)
        ctx.project_listeners.append(self._on_project)

    # ------------------------------------------------------------------ 뼈대
    def _scroll(self, content: QWidget) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(content)
        scroll.viewport().setAutoFillBackground(False)
        content.setAutoFillBackground(False)
        return scroll

    def _column(self) -> Tuple[QWidget, QVBoxLayout]:
        w = QWidget()
        col = QVBoxLayout(w)
        col.setContentsMargins(0, 4, 6, 4)
        col.setSpacing(14)
        return w, col

    def _empty_view(self) -> QWidget:
        w = QWidget()
        col = QVBoxLayout(w)
        col.addStretch(1)
        self.empty_text = label("등록된 프로젝트가 없습니다.", "emptyText")
        self.empty_text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        col.addWidget(self.empty_text)
        row = QHBoxLayout()
        row.addStretch(1)
        add = button("+  새 프로젝트 추가", "primary")
        add.clicked.connect(self.add_project)
        imp = button("기존 툴에서 가져오기")
        imp.clicked.connect(self.import_legacy)
        row.addWidget(add)
        row.addWidget(imp)
        row.addStretch(1)
        col.addSpacing(10)
        col.addLayout(row)
        col.addStretch(2)
        return w

    def _status_bar(self) -> QWidget:
        frame = QFrame()
        frame.setObjectName("preflight")
        row = QHBoxLayout(frame)
        row.setContentsMargins(12, 8, 12, 8)
        row.setSpacing(10)
        self.status_chip = Chip("", "neutral")
        self.status_text = label("", "muted")
        self.status_text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        row.addWidget(self.status_chip, 0, Qt.AlignmentFlag.AlignTop)
        row.addWidget(self.status_text, 1)
        return frame

    def _watch(self, *widgets: QWidget) -> None:
        for w in widgets:
            if isinstance(w, PathField):
                w = w.edit
            if isinstance(w, QLineEdit):
                w.textChanged.connect(self._changed)
            elif isinstance(w, QPlainTextEdit):
                w.textChanged.connect(self._changed)
            elif isinstance(w, QComboBox):
                # 긴 항목 글자 때문에 화면이 옆으로 넓어지지 않게 (펼친 목록에서는 전체가 보인다)
                w.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
                w.setMinimumContentsLength(16)
                w.currentIndexChanged.connect(self._changed)
            elif isinstance(w, RowTable):
                w.changed.connect(self._changed)

    # ------------------------------------------------------------------ 탭: 기본
    def _basic_tab(self) -> QWidget:
        w, col = self._column()
        card = Card("기본", "비워 둔 칸은 기본값을 따릅니다. 흐린 글씨가 지금 쓰이는 값입니다.")
        form = _form()
        self.name_edit = QLineEdit()
        self.root_field = PathField(placeholder="로컬 git 저장소 최상위 폴더")
        self.output_field = PathField()
        self.os_combo = QComboBox()
        self.os_combo.addItem(INHERIT, "")
        self.os_combo.addItem("리눅스 (shipkit.sh)", "linux")
        self.os_combo.addItem("Windows (shipkit.ps1)", "windows")
        self.server_edit = QLineEdit()
        self.gitlab_edit = QLineEdit()
        self.github_edit = QLineEdit()
        self.package_name_edit = QLineEdit()
        for text, widget in (("이름", self.name_edit), ("로컬 경로", self.root_field),
                             ("패키지 생성 경로", self.output_field), ("서버 OS", self.os_combo),
                             ("서버 배포 경로", self.server_edit), ("GitLab 프로젝트", self.gitlab_edit),
                             ("GitHub 저장소", self.github_edit), ("패키지 폴더 이름", self.package_name_edit)):
            form.addRow(label(text, "fieldLabel", wrap=False), widget)
        card.body.addLayout(form)
        self.basic_note = label("", "muted")
        card.body.addWidget(self.basic_note)
        col.addWidget(card)
        self._watch(self.name_edit, self.root_field, self.output_field, self.os_combo, self.server_edit,
                    self.gitlab_edit, self.github_edit, self.package_name_edit)

        danger = Card("프로젝트 지우기", "목록에서만 지웁니다. 로컬 저장소와 만든 패키지는 그대로 둡니다.")
        delete = button("이 프로젝트 지우기", "danger")
        delete.clicked.connect(self.delete_project)
        row = QHBoxLayout()
        row.addWidget(delete)
        row.addStretch(1)
        danger.body.addLayout(row)
        col.addWidget(danger)
        col.addStretch(1)
        return w

    # ------------------------------------------------------------------ 탭: 반영 규칙
    def _rules_tab(self) -> QWidget:
        w, col = self._column()
        card = Card("배포 방식", "프리셋은 언어·빌드 도구별로 미리 정한 규칙 묶음입니다.")
        form = _form()
        self.preset_combo = QComboBox()
        self.preset_combo.addItem(INHERIT, "")
        for key, preset in PRESETS.items():
            self.preset_combo.addItem(f"{key}  -  {preset['description']}", key)
        self.mode_combo = QComboBox()
        self.mode_combo.addItem(INHERIT, "")
        self.mode_combo.addItem("변경 파일 매핑 (changes) - 바뀐 파일을 규칙대로 서버 위치에", "changes")
        self.mode_combo.addItem("빌드 결과 동기화 (sync) - 빌드 폴더 전체를 서버와 맞춤", "sync")
        form.addRow(label("프리셋", "fieldLabel", wrap=False), self.preset_combo)
        form.addRow(label("방식", "fieldLabel", wrap=False), self.mode_combo)
        card.body.addLayout(form)
        col.addWidget(card)
        self._watch(self.preset_combo, self.mode_combo)

        self.mode_stack = QStackedWidget()
        # changes: 규칙 표
        changes = Card("반영 규칙", "저장소의 소스 폴더를 서버 배포 경로 아래 어디에 둘지 정합니다. "
                                 "빌드 결과 폴더를 적으면 .java 대신 그 폴더의 .class 를 넣습니다. ( . = 전체 )")
        self.rules_override = OverrideBox("반영 규칙")
        self.rules_override.toggled.connect(self._rules_toggled)
        self.rules_table = RowTable([("src", "소스 폴더"), ("dest", "서버 위치"), ("build_dir", "빌드 결과 폴더")],
                                    stretch=2, height=152)
        self.modules_edit = QLineEdit()
        self.modules_edit.setPlaceholderText(". (저장소 전체)  -  멀티 모듈이면 web, admin 처럼 쉼표로")
        mod_form = _form()
        mod_form.addRow(label("모듈 폴더", "fieldLabel", wrap=False), self.modules_edit)
        changes.body.addWidget(self.rules_override)
        changes.body.addWidget(self.rules_table)
        changes.body.addLayout(mod_form)
        self._watch(self.rules_table, self.modules_edit)
        # sync: 빌드 폴더
        sync = Card("빌드 결과 동기화", "빌드 결과 폴더 전체를 패키지에 담고, 서버에서는 내용이 다른 파일만 바꿉니다.")
        sync_form = _form()
        self.build_dir_edit = QLineEdit()
        self.dest_edit = QLineEdit()
        self.prune_edit = QLineEdit()
        for text, widget in (("빌드 결과 폴더", self.build_dir_edit), ("서버 위치", self.dest_edit),
                             ("옛 파일 정리 폴더", self.prune_edit)):
            sync_form.addRow(label(text, "fieldLabel", wrap=False), widget)
        sync.body.addLayout(sync_form)
        sync.body.addWidget(label("옛 파일 정리 폴더: 이번 빌드에 없는 파일을 서버에서 지울 폴더 (쉼표로 여러 개). "
                                  "파일 이름이 빌드마다 바뀌는 assets/ 같은 폴더에 씁니다.", "muted"))
        self._watch(self.build_dir_edit, self.dest_edit, self.prune_edit)
        self.mode_stack.addWidget(changes)
        self.mode_stack.addWidget(sync)
        col.addWidget(self.mode_stack)

        # 경로 시험
        test = Card("경로 시험", "파일 경로를 넣으면 지금 규칙으로 서버 어디에 들어가는지 보여 줍니다. (저장하지 않은 값으로 시험)")
        self.test_input = LineList("예: src/main/java/com/acme/UserController.java\n"
                                   "    src/main/webapp/WEB-INF/jsp/user/list.jsp\n"
                                   "    D\tsrc/main/webapp/old.jsp   (앞에 D 와 탭이면 삭제)", height=84)
        self.test_btn = button("시험", "primary")
        self.test_btn.clicked.connect(self.run_path_test)
        row = QHBoxLayout()
        row.addWidget(self.test_input, 1)
        row.addWidget(self.test_btn, 0, Qt.AlignmentFlag.AlignTop)
        test.body.addLayout(row)
        self.test_tree = QTreeWidget()
        self.test_tree.setHeaderLabels(["결과", "파일", "서버 위치 · 메시지"])
        self.test_tree.setRootIsDecorated(True)
        self.test_tree.setMinimumHeight(170)
        self.test_tree.setColumnWidth(0, 80)
        self.test_tree.setColumnWidth(1, 380)
        self.test_tree.setItemDelegateForColumn(0, StatusDelegate(self.test_tree))
        self.test_tree.hide()
        self.test_note = label("", "muted")
        self.test_note.hide()
        test.body.addWidget(self.test_note)
        test.body.addWidget(self.test_tree)
        col.addWidget(test)
        col.addStretch(1)
        return w

    def _rules_toggled(self, on: bool) -> None:
        if on and not self.rules_table.rows():
            self.rules_table.set_rows(self._display_rules(self._inherited_rules()[0]))
        self.rules_override.set_source(self.rules_override.source)
        self._refresh_inherited()
        self._changed()

    @staticmethod
    def _display_rules(rules: Sequence[Dict[str, Any]]) -> List[Dict[str, str]]:
        return [{"src": _show_prefix(str(r.get("src") or "")), "dest": _show_prefix(str(r.get("dest") or "")),
                 "build_dir": r.get("build_dir") or ""} for r in rules]

    # ------------------------------------------------------------------ 탭: 서버 명령
    def _hooks_tab(self) -> QWidget:
        w, col = self._column()
        card = Card("서버 명령 (선택)",
                    "배포할 때마다 서버에서 손으로 치던 명령(톰캣 재시작 등)이 있으면 적어 두세요. 서버에서 "
                    "./shipkit.sh apply 를 실행하면 파일 반영에 이어 이 명령도 실행합니다. 없으면 비워 두면 됩니다.")
        example = QFrame()
        example.setObjectName("detail")
        ex = QVBoxLayout(example)
        ex.setContentsMargins(14, 10, 14, 10)
        ex.setSpacing(6)
        ex.addWidget(label("예: '반영 후'에 sudo systemctl restart tomcat 을 적으면 서버에서 이렇게 동작합니다. "
                           "실행 전에 꼭 묻고, y 를 눌러야 실행합니다.", "muted"))
        ex.addWidget(label(HOOK_EXAMPLE, "code", wrap=False))
        card.body.addWidget(example)
        card.body.addWidget(label("조건 칸 (선택): 이 패턴에 맞는 파일이 바뀐 배포에만 명령을 넣습니다. 예를 들어 **/*.java 를 "
                                  "적으면 Java 소스가 바뀐 배포에서만 재시작하고, JSP·JS 만 바뀐 배포에서는 재시작하지 않습니다. "
                                  "비워 두면 매번 넣습니다.", "muted"))
        self.hooks_override = OverrideBox("서버 명령")
        self.hooks_override.toggled.connect(self._hooks_toggled)
        card.body.addWidget(self.hooks_override)
        self.hook_tables: Dict[str, RowTable] = {}
        for stage, title, hint, placeholder in HOOK_STAGES:
            head = QHBoxLayout()
            head.setSpacing(10)
            head.addWidget(label(title, "fieldLabel", wrap=False))
            head.addWidget(label(hint, "muted", wrap=False), 1)
            card.body.addSpacing(4)
            card.body.addLayout(head)
            table = RowTable([("run", "명령"), ("when", "조건 (선택)")], stretch=0, height=112,
                             placeholder=placeholder)
            table.table.setColumnWidth(1, 220)
            self.hook_tables[stage] = table
            card.body.addWidget(table)
            self._watch(table)
        col.addWidget(card)

        self.server_card = Card("새로 생기는 파일의 소유자·권한 (선택, 리눅스 서버)",
                                "배포로 서버에 처음 생기는 파일·폴더(새 JSP 등)에만 적용합니다. 이미 있던 파일은 원래 "
                                "소유자·권한을 그대로 둡니다. 비워 두면 apply 를 실행한 계정 소유가 되고 권한은 서버 기본값을 따릅니다.")
        form = _form()
        self.owner_edit = QLineEdit()
        self.file_mode_edit = QLineEdit()
        self.dir_mode_edit = QLineEdit()
        self.history_edit = QLineEdit()
        for text, widget in (("소유자", self.owner_edit), ("파일 권한", self.file_mode_edit),
                             ("폴더 권한", self.dir_mode_edit), ("배포 이력 파일", self.history_edit)):
            form.addRow(label(text, "fieldLabel", wrap=False), widget)
        self.server_card.body.addLayout(form)
        self.server_note = label("", "muted")
        self.server_card.body.addWidget(self.server_note)
        col.addWidget(self.server_card)
        self._watch(self.owner_edit, self.file_mode_edit, self.dir_mode_edit, self.history_edit)
        col.addStretch(1)
        return w

    def _hooks_toggled(self, on: bool) -> None:
        if on and not any(t.rows() for t in self.hook_tables.values()):
            self._set_hook_rows(self.layers.inherited("hooks")[0] if self.layers else None)
        self.hooks_override.set_source(self.hooks_override.source)
        self._refresh_inherited()
        self._changed()

    def _set_hook_rows(self, hooks: Any) -> None:
        hooks = hooks or {}
        for stage, table in self.hook_tables.items():
            items = hooks.get(stage) or []
            if isinstance(items, (str, dict)):
                items = [items]
            table.set_rows([{"run": i} if isinstance(i, str) else {"run": i.get("run"), "when": i.get("when")}
                            for i in items])

    # ------------------------------------------------------------------ 탭: 제외·알림
    def _filters_tab(self) -> QWidget:
        w, col = self._column()
        exclude = Card("배포에서 뺄 파일", "아래 이미 빼는 파일에 더해집니다. 한 줄에 하나씩 적습니다 (예: docs/**, **/*.bak).")
        self.exclude_inherited = label("", "muted")
        self.exclude_list = LineList("docs/**")
        exclude.body.addWidget(self.exclude_inherited)
        exclude.body.addWidget(self.exclude_list)
        col.addWidget(exclude)

        sensitive = Card("민감 파일", "실수로 배포되면 안 되는 파일은 막습니다. 꼭 배포해야 하는 파일은 예외로 허용하세요.")
        self.deny_inherited = label("", "muted")
        sensitive.body.addWidget(self.deny_inherited)
        lists = QHBoxLayout()
        lists.setSpacing(12)
        self.deny_list = LineList("더 막을 패턴  (예: **/secret*.properties)")
        self.allow_list = LineList("예외로 허용할 패턴  (예: config/.env.dev)")
        for title, widget in (("더 막기", self.deny_list), ("예외 허용", self.allow_list)):
            box = QVBoxLayout()
            box.setSpacing(4)
            box.addWidget(label(title, "fieldLabel", wrap=False))
            box.addWidget(widget)
            lists.addLayout(box, 1)
        sensitive.body.addLayout(lists)
        col.addWidget(sensitive)

        notices = Card("알림", "이 패턴의 파일이 바뀌면 미리보기와 보고서에 알림을 띄웁니다 (예: DB 반영, 라이브러리 추가).")
        self.notices_inherited = label("", "muted")
        self.notices_table = RowTable([("pattern", "파일 패턴"), ("message", "알림 내용")], stretch=1, height=112)
        self.notices_table.table.setColumnWidth(0, 220)
        notices.body.addWidget(self.notices_inherited)
        notices.body.addWidget(self.notices_table)
        col.addWidget(notices)
        self._watch(self.exclude_list, self.deny_list, self.allow_list, self.notices_table)
        col.addStretch(1)
        return w

    # ------------------------------------------------------------------ 불러오기
    def _on_project(self) -> None:
        """작업 프로젝트가 바뀌거나 설정을 다시 읽었을 때."""
        if self.dirty and self.loaded_name:
            if self.loaded_name == self.ctx.current and self.loaded_name in self.ctx.project_names():
                # 다른 화면에서 설정을 저장했을 때: 고치던 값은 두고 바뀐 기본값으로 다시 검증한다
                self.data = config_store.load_raw(self.ctx.config_path)
                self._changed()
                return
            if self.ctx.dialogs.ask("저장하지 않은 변경",
                                    f"'{self.loaded_name}' 프로젝트 설정에 저장하지 않은 변경이 있습니다.\n저장할까요?"):
                if self.save(select=self.ctx.current):
                    return
        self.load()

    def load(self) -> None:
        name = self.ctx.current
        self.dirty = False
        self.loaded_name = name
        if not name:
            self.layers = None
            self.empty_text.setText(f"설정 파일 오류: {self.ctx.load_error}" if self.ctx.load_error
                                    else "등록된 프로젝트가 없습니다.")
            self.subtitle.setText("")
            self.body.setCurrentIndex(0)
            self._update_buttons()
            return
        self.body.setCurrentIndex(1)
        try:
            self.data = config_store.load_raw(self.ctx.config_path)
        except Exception as e:  # 설정 파일을 읽지 못하면 ctx.load_error 로 이미 안내됨
            self.data = {}
            self.ctx.dialogs.error("설정 파일 오류", str(e))
        self.layers = project_edit.load_layers(self.data, name, self.ctx.config_path)
        e = self.layers.entry
        self._loading = True
        self.subtitle.setText(f"{name}  ·  화면에서 바꾼 값은 이 PC 의 개인 설정({CONFIG_NAME})에 저장됩니다")
        self.name_edit.setText(name)
        self.root_field.setText(str(e.get("root", "")))
        self.output_field.setText(str(e.get("output_dir", "")))
        self._select(self.os_combo, e.get("server_os"))
        self.server_edit.setText(str(e.get("server_path", "")))
        self.gitlab_edit.setText(str(e.get("gitlab_project", "")))
        self.github_edit.setText(str(e.get("github_repo", "")))
        self.package_name_edit.setText(str(e.get("package_name", "")))
        self._select(self.preset_combo, e.get("preset"))
        self._select(self.mode_combo, e.get("mode"))
        self.rules_override.set_overridden("rules" in e)
        self.rules_table.set_rows(self._display_rules(e.get("rules") or []))
        self.modules_edit.setText(", ".join(str(m) for m in e.get("modules") or []))
        self.build_dir_edit.setText(str(e.get("build_dir", "")))
        self.dest_edit.setText(str(e.get("dest", "")))
        self.prune_edit.setText(", ".join(str(p) for p in e.get("prune") or []))
        self.hooks_override.set_overridden("hooks" in e)
        self._set_hook_rows(e.get("hooks"))
        server = e.get("server") or {}
        self.owner_edit.setText(str(server.get("owner", "")))
        self.file_mode_edit.setText(str(server.get("file_mode", "")))
        self.dir_mode_edit.setText(str(server.get("dir_mode", "")))
        self.history_edit.setText(str(server.get("history_file", "")))
        self.exclude_list.set_lines(e.get("exclude") or [])
        self.deny_list.set_lines(e.get("deny") or [])
        self.allow_list.set_lines(e.get("allow") or [])
        self.notices_table.set_rows(e.get("notices") or [])
        shared_servers = self.layers.inherited("servers")[0] or []
        self.servers_tab.load(e.get("servers") or shared_servers,
                              "저장소의 .shipkit.yml 에 있는 서버입니다. 고치면 이 PC 의 개인 설정에 저장됩니다."
                              if shared_servers and not e.get("servers") else "")
        self.test_tree.clear()
        self.test_tree.hide()
        self.test_note.hide()
        self._loading = False
        self._refresh_inherited()
        self.baseline = self.collect_entry()  # 이 값과 달라지면 '저장하지 않음'
        self._validate()

    @staticmethod
    def _select(combo: QComboBox, value: Any) -> None:
        idx = combo.findData(value or "")
        combo.setCurrentIndex(idx if idx >= 0 else 0)

    # ------------------------------------------------------------------ 편집 중 값
    def collect_entry(self) -> Dict[str, Any]:
        """화면의 값으로 만든 개인 설정 프로젝트 항목. 화면에 없는 키(source_encoding 등)는 그대로 둔다."""
        entry = copy.deepcopy(self.layers.entry) if self.layers else {}

        def put(key: str, value: Any) -> None:
            if value in (None, "", [], {}):
                entry.pop(key, None)
            else:
                entry[key] = value

        put("root", self.root_field.text())
        put("output_dir", self.output_field.text())
        put("server_os", self.os_combo.currentData())
        put("server_path", self.server_edit.text().strip())
        put("gitlab_project", self.gitlab_edit.text().strip())
        put("github_repo", self.github_edit.text().strip())
        put("package_name", self.package_name_edit.text().strip())
        put("preset", self.preset_combo.currentData())
        put("mode", self.mode_combo.currentData())
        put("modules", _split(self.modules_edit.text()))
        put("build_dir", self.build_dir_edit.text().strip())
        put("dest", self.dest_edit.text().strip())
        put("prune", _split(self.prune_edit.text()))
        if self.rules_override.overridden():
            rules = [{k: v for k, v in r.items() if v or k in ("src", "dest")} for r in self.rules_table.rows()]
            put("rules", rules)
        else:
            entry.pop("rules", None)
        hooks = {}
        for stage, table in self.hook_tables.items():
            items = [r["run"] if not r.get("when") else {"run": r["run"], "when": r["when"]}
                     for r in table.rows()]
            if items:
                hooks[stage] = items
        if self.hooks_override.overridden() and (hooks or self.hooks_override.source):
            entry["hooks"] = hooks  # 물려받는 명령을 이 PC 에서 끄려면 빈 값도 저장한다
        else:
            entry.pop("hooks", None)
        server = {k: w.text().strip() for k, w in (("owner", self.owner_edit), ("file_mode", self.file_mode_edit),
                                                   ("dir_mode", self.dir_mode_edit),
                                                   ("history_file", self.history_edit)) if w.text().strip()}
        if server:
            # server 는 층마다 통째로 덮어쓰므로, 팀 공유 값 가운데 비워 둔 칸은 그대로 이어 받는다
            inherited = (self.layers.inherited("server")[0] if self.layers else None) or {}
            server = {**{k: v for k, v in inherited.items() if v not in (None, "")}, **server}
        put("server", server)
        put("exclude", self.exclude_list.lines())
        put("deny", self.deny_list.lines())
        put("allow", self.allow_list.lines())
        put("notices", self.notices_table.rows())
        shared_servers = (self.layers.inherited("servers")[0] if self.layers else None) or []
        if self.servers_tab.servers == shared_servers and "servers" not in (self.layers.entry if self.layers else {}):
            entry.pop("servers", None)   # 저장소 값 그대로면 개인 설정에 따로 두지 않는다
        else:
            put("servers", copy.deepcopy(self.servers_tab.servers))
        return entry

    def _changed(self, *_) -> None:
        if self._loading or not self.layers:
            return
        self._refresh_inherited()
        self.dirty = (self.collect_entry() != self.baseline
                      or self.name_edit.text().strip() != (self.loaded_name or ""))
        self._validate()

    def _inherited_rules(self) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        entry = {k: v for k, v in self.collect_entry().items() if k != "rules"}
        return self.layers.inherited_rules(entry) if self.layers else ([], None)

    def _refresh_inherited(self) -> None:
        """비워 둔 칸에 지금 쓰이는 값(팀 공유·프리셋·기본값)을 흐린 글씨로 보여 준다."""
        if not self.layers:
            return
        name = self.name_edit.text().strip() or self.loaded_name or ""
        entry = self.collect_entry()
        # 로컬 경로를 바꾸면 팀 공유 파일 위치도 바뀐다
        self.layers = project_edit.load_layers(project_edit.with_entry(self.data, self.loaded_name, entry),
                                               self.loaded_name, self.ctx.config_path)
        L = self.layers

        def ph(edit: QLineEdit, key: str, fallback: str = "") -> None:
            value, source = L.inherited(key)
            if isinstance(value, list):
                value = ", ".join(str(v) for v in value)
            edit.setPlaceholderText(f"{source}: {value}" if value not in (None, "") else fallback)

        def combo_text(combo: QComboBox, text: str) -> None:
            combo.setItemText(0, f"(따름: {text})" if text else INHERIT)

        ph(self.root_field.edit, "root", "로컬 git 저장소 최상위 폴더")
        out, src = L.inherited("output_dir")
        self.output_field.edit.setPlaceholderText(
            f"{src}: {str(out).replace('{project}', name)}" if out else "[앱 설정]에서 기본 패키지 생성 경로를 정하세요")
        os_value, os_src = L.inherited("server_os")
        combo_text(self.os_combo, f"{os_src} · {'Windows' if os_value == 'windows' else '리눅스'}" if os_value else "리눅스")
        ph(self.server_edit, "server_path", "예: /app/tomcat/webapps/ROOT  (Windows: D:/tomcat/webapps/ROOT)")
        ph(self.gitlab_edit, "gitlab_project", "GitLab 프로젝트 ID 또는 그룹/프로젝트")
        ph(self.github_edit, "github_repo", "owner/name")
        ph(self.package_name_edit, "package_name", f"기본값: {DEFAULT_PACKAGE_NAME}   (예: 2026-10-26-1133)")
        preset_value, preset_src = L.inherited("preset")
        effective = L.preset_name(entry)
        combo_text(self.preset_combo, f"{preset_src} · {preset_value}" if preset_value else
                   (f"{effective} (기본)" if effective and not entry.get("rules") else ""))
        mode = self._effective_mode(entry)
        mode_value, mode_src = L.inherited("mode")
        combo_text(self.mode_combo, f"{mode_src} · {MODE_TEXT.get(mode_value, mode_value)}" if mode_value else
                   f"프리셋 · {MODE_TEXT[mode]}")
        self.mode_stack.setCurrentIndex(1 if mode == "sync" else 0)

        # 반영 규칙
        rules, rules_src = L.inherited_rules({k: v for k, v in entry.items() if k != "rules"})
        self.rules_override.set_source(rules_src)
        if not self.rules_override.overridden():
            self.rules_table.set_rows(self._display_rules(rules))
        self.rules_table.set_editable(self.rules_override.overridden())
        ph(self.modules_edit, "modules", ". (저장소 전체)  -  멀티 모듈이면 web, admin 처럼 쉼표로")
        preset = PRESETS.get(effective or "", {})
        for edit, key in ((self.build_dir_edit, "build_dir"), (self.prune_edit, "prune")):
            value, source = L.inherited(key)
            value = value if value not in (None, "") else preset.get(key)
            source = source or (f"프리셋 {effective}" if preset.get(key) else None)
            if isinstance(value, list):
                value = ", ".join(value)
            edit.setPlaceholderText(f"{source}: {value}" if value else
                                    ("예: dist" if key == "build_dir" else "예: assets"))
        ph(self.dest_edit, "dest", "비우면 서버 배포 경로 바로 아래")

        # 서버 명령
        hooks, hooks_src = L.inherited("hooks")
        self.hooks_override.set_source(hooks_src)
        if not self.hooks_override.overridden():
            self._set_hook_rows(hooks)
        for table in self.hook_tables.values():
            table.set_editable(self.hooks_override.overridden())
        server, server_src = L.inherited("server")
        server = server or {}
        for edit, key, fallback in ((self.owner_edit, "owner", "예: tomcat:tomcat   (inherit = 위 폴더와 같은 소유자)"),
                                    (self.file_mode_edit, "file_mode", "예: 644   (주인은 읽기·쓰기, 나머지는 읽기만)"),
                                    (self.dir_mode_edit, "dir_mode", "예: 755"),
                                    (self.history_edit, "history_file",
                                     f"기본값: {DEFAULT_HISTORY_FILE}   (apply·rollback 기록을 남기는 서버 파일)")):
            value = server.get(key)
            edit.setPlaceholderText(f"{server_src}: {value}" if value not in (None, "") else fallback)
        windows = (self.os_combo.currentData() or os_value) == "windows"
        for edit in (self.owner_edit, self.file_mode_edit, self.dir_mode_edit):
            edit.setEnabled(not windows)
        self.server_note.setText("Windows 서버는 소유자·권한을 지정하지 않습니다 (위 폴더 권한을 따름). 배포 이력 파일만 바꿀 수 있습니다."
                                 if windows else
                                 "소유자를 바꾸려면 apply 를 실행하는 계정에 그 권한(sudo 등)이 있어야 합니다.")

        # 제외·알림 (합쳐지는 값)
        base = list(_COMMON_EXCLUDE) + list(preset.get("exclude", []))
        excl = base + [str(x) for x in L.inherited_list("exclude")]
        self.exclude_inherited.setText("이미 빼는 파일: " + "  ·  ".join(dict.fromkeys(excl)))
        deny = list(DEFAULT_DENY) + [str(x) for x in L.inherited_list("deny")]
        allow = list(DEFAULT_ALLOW) + [str(x) for x in L.inherited_list("allow")]
        self.deny_inherited.setText("기본으로 막는 파일: " + "  ·  ".join(dict.fromkeys(deny))
                                    + "<br>기본으로 허용: " + "  ·  ".join(dict.fromkeys(allow)))
        notes = [n["message"] for n in preset.get("notices", [])]
        notes += [str((n or {}).get("message", "")) for n in L.inherited_list("notices")]
        self.notices_inherited.setText(("이미 있는 알림: " + "  ·  ".join(notes)) if notes else "")
        self.notices_inherited.setVisible(bool(notes))
        # 저장소에 .shipkit.yml 이 있으면 그 값도 쓰인다 (흐린 글씨의 '팀 공유')
        self.basic_note.setText(f"저장소의 {PROJECT_FILE} 값도 함께 씁니다: {L.shared_path}" if L.has_shared else "")
        self.basic_note.setVisible(L.has_shared)

    def _effective_mode(self, entry: Dict[str, Any]) -> str:
        if entry.get("mode"):
            return entry["mode"]
        value, _ = self.layers.inherited("mode")
        if value:
            return value
        preset = self.layers.preset_name(entry)
        if preset:
            return PRESETS[preset]["mode"]
        return "sync" if entry.get("build_dir") or self.layers.inherited("build_dir")[0] else "changes"

    def _validate(self) -> None:
        name = self.name_edit.text().strip()
        try:
            _, self.draft = project_edit.draft_project(self.data, name, self.collect_entry(), self.ctx.config_path,
                                                       self.loaded_name)
        except ConfigError as e:
            self.draft = None
            self.status_chip.set("설정 오류", "danger")
            self.status_text.setText(str(e))
        else:
            p = self.draft
            server = f"{p.server_path}" + (" (Windows)" if p.server_os == "windows" else "")
            shared = f"  ·  {PROJECT_FILE} 사용" if p.project_file else ""
            self.status_chip.set("저장하지 않음" if self.dirty else "설정 정상", "warning" if self.dirty else "success")
            self.status_text.setText(f"{p.describe_mode}  ·  서버 {server}{shared}")
        self._update_buttons()

    def _update_buttons(self) -> None:
        self.save_btn.setEnabled(self.dirty and self.draft is not None)
        self.revert_btn.setEnabled(self.dirty)

    # ------------------------------------------------------------------ 경로 시험
    def run_path_test(self) -> None:
        self.test_tree.clear()
        p = self.draft
        lines = self.test_input.lines()
        note = ""
        if p is None:
            note = "설정 오류를 먼저 고치세요: " + self.status_text.text()
        elif p.mode == "sync":
            note = (f"빌드 결과 동기화 방식은 {p.build_dir}/ 전체를 서버 {posixpath.join(p.server_path, p.dest) if p.dest else p.server_path}"
                    " 와 맞춥니다. 경로 시험은 변경 파일 매핑 방식에서 씁니다.")
        elif not lines:
            note = "시험할 파일 경로를 한 줄에 하나씩 넣으세요."
        if note:
            self.test_note.setText(note)
            self.test_note.show()
            self.test_tree.hide()
            return
        try:
            changes, _ = workflow.collect_lines(lines, "경로 시험")
        except InputError as e:
            self.test_note.setText(str(e))
            self.test_note.show()
            self.test_tree.hide()
            return
        entries = mapper.plan(p, changes)
        for e in entries:
            top = QTreeWidgetItem([STATUS_TEXT[e.status], f"{e.change.change.label}  {e.change.path}", ""])
            top.setData(0, Qt.ItemDataRole.UserRole, STATUS_TONE[e.status])
            top.setToolTip(1, e.change.path)
            for a in e.artifacts:
                QTreeWidgetItem(top, ["", "", f"→  {_server_target(p, a.target)}"])
            for d in e.deletes:
                QTreeWidgetItem(top, ["", "", f"(삭제)  {_server_target(p, d)}"])
            for m in e.messages:
                child = QTreeWidgetItem(top, ["", "", m])
                tone = {Status.ERROR: "danger", Status.WARN: "warning"}.get(e.status, "neutral")
                child.setForeground(2, QColor(theme.TONES[tone][0]))
            if not e.artifacts and not e.deletes and not e.messages:
                QTreeWidgetItem(top, ["", "", "서버에 넣을 파일 없음"])
            self.test_tree.addTopLevelItem(top)
            top.setExpanded(True)
        self.test_note.hide()
        self.test_tree.show()

    # ------------------------------------------------------------------ 저장·되돌리기·추가·삭제
    def save(self, select: Optional[str] = None) -> bool:
        if not self.layers or not self.loaded_name:
            return False
        name = self.name_edit.text().strip()
        try:
            data = config_store.load_raw(self.ctx.config_path)
            project_edit.draft_project(data, name, self.collect_entry(), self.ctx.config_path, self.loaded_name)
            config_store.save_raw(self.ctx.config_path,
                                  project_edit.with_entry(data, name, self.collect_entry(), self.loaded_name))
        except ConfigError as e:
            self.ctx.dialogs.error("저장하지 못했습니다", str(e))
            return False
        self.dirty = False
        self.ctx.reload(select=select or name)
        return True

    def revert(self) -> None:
        self.dirty = False
        self.load()

    def _save(self, mutate, select: Optional[str] = None) -> bool:
        """설정을 고쳐 저장하고 다시 읽는다. select: 저장 뒤 작업 프로젝트로 고를 이름."""
        data = config_store.load_raw(self.ctx.config_path)
        before = copy.deepcopy(data)
        try:
            mutate(data)
            config_store.save_raw(self.ctx.config_path, data)
        except ConfigError as e:
            self.ctx.dialogs.error("저장하지 못했습니다", str(e))
            return False
        if data != before:
            self.dirty = False
            self.ctx.reload(select)
        return True

    def add_project(self) -> None:
        data = config_store.load_raw(self.ctx.config_path)
        dialog = self.dialog_factory(self, "", {}, data.get("defaults") or {})
        while dialog.exec():
            if self._save(lambda d: config_store.set_project(d, dialog.name(), dialog.values()), select=dialog.name()):
                self.tabs.setCurrentIndex(TAB_BASIC)
                return

    def delete_project(self) -> None:
        name = self.loaded_name
        if not name:
            return
        if self.ctx.dialogs.ask("삭제 확인", f"'{name}' 프로젝트를 목록에서 지울까요?\n(로컬 저장소와 파일은 그대로 둡니다)"):
            self.dirty = False
            self._save(lambda d: config_store.delete_project(d, name))

    def import_legacy(self) -> None:
        db = legacy.legacy_db_path()
        if not db.is_file():
            self.ctx.dialogs.error("기존 툴 설정 없음", f"기존 툴(compiled-java-deployer) DB 가 없습니다:\n{db}")
            return
        result: Dict[str, Any] = {}

        def mutate(data):
            result["added"], result["skipped"] = config_store.merge_legacy(data, db)

        if self._save(mutate):
            added, skipped = result["added"], result["skipped"]
            text = f"{len(added)}개 프로젝트를 가져왔습니다." + (f"\n이미 있어서 건너뜀: {', '.join(skipped)}" if skipped else "")
            text += "\n토큰은 가져오지 않습니다. [연결] 메뉴에서 토큰을 저장하세요."
            self.ctx.dialogs.info("가져오기 완료", text)
