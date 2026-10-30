"""처음 실행 안내: ① 연결(GitLab) → ② 패키지 위치 → ③ 프로젝트. 끝나면 바로 배포 화면에서 쓸 수 있다.

기존 배포 툴(compiled-java-deployer) 설정이 있으면 첫 화면에서 가져올 수 있다 (프로젝트·GitLab 주소·기본 경로).
마지막 [완료]에서 설정 파일을 검증해 저장하고, 토큰은 Windows 자격 증명 관리자에 넣는다.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QStackedWidget,
    QVBoxLayout, QWidget,
)

from shipkit import PROJECT_FILE, __app_name__, config_store
from shipkit.config import PRESETS, ConfigError, GitLabConfig, load_project_file
from shipkit.gui import theme
from shipkit.gui.context import AppContext
from shipkit.gui.widgets import Card, PathField, Stepper, button, label
from shipkit.projectfile import detect_preset, find_repo_root
from shipkit.secrets import SecretError, set_token
from shipkit.sources import git_local
from shipkit.sources.gitlab import GitLabClient

STEP_NAMES = ("연결", "패키지 위치", "프로젝트")
STEP_CONNECT, STEP_OUTPUT, STEP_PROJECT = range(3)


def _form() -> QFormLayout:
    form = QFormLayout()
    form.setHorizontalSpacing(16)
    form.setVerticalSpacing(10)
    form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    return form


def suggest_output_dir() -> str:
    """패키지 생성 경로 제안: D 드라이브가 있으면 D:/deploy, 없으면 사용자 폴더 아래."""
    if os.name == "nt" and Path("D:/").is_dir():
        return "D:/deploy/{project}"
    return (Path.home() / "deploy").as_posix() + "/{project}"


class SetupWizard(QDialog):
    def __init__(self, ctx: AppContext, parent: Optional[QWidget] = None, legacy_db: Optional[Path] = None):
        super().__init__(parent)
        self.ctx = ctx
        self.legacy_db = legacy_db if legacy_db and legacy_db.is_file() else None
        self.setWindowTitle(f"{__app_name__} 시작하기")
        self.setMinimumSize(760, 600)
        self.step = STEP_CONNECT
        self.imported: List[str] = []        # 기존 툴에서 가져온 프로젝트
        self.legacy_data: Dict[str, Any] = {}
        self.selected: Optional[str] = None  # 끝난 뒤 작업 프로젝트로 고를 이름
        self._name_edited = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 20)
        layout.setSpacing(14)
        head = QHBoxLayout()
        logo = QLabel()
        theme.bind_logo(logo, 40)  # 테마에 맞는 색 (어두운 테마면 어두운 바탕·밝은 선)
        texts = QVBoxLayout()
        texts.setSpacing(2)
        texts.addWidget(label(f"{__app_name__} 시작하기", "pageTitle"))
        texts.addWidget(label("세 가지만 정하면 바로 배포 패키지를 만들 수 있습니다. 모두 나중에 바꿀 수 있습니다.",
                              "pageSubtitle"))
        head.addWidget(logo, 0, Qt.AlignmentFlag.AlignTop)
        head.addSpacing(8)
        head.addLayout(texts, 1)
        layout.addLayout(head)
        self.stepper = Stepper(STEP_NAMES)
        self.stepper.clicked.connect(self.go)
        layout.addWidget(self.stepper, 0, Qt.AlignmentFlag.AlignLeft)

        self.pages = QStackedWidget()
        self.pages.addWidget(self._connect_page())
        self.pages.addWidget(self._output_page())
        self.pages.addWidget(self._project_page())
        layout.addWidget(self.pages, 1)

        self.error = label("", "")
        self.error.setStyleSheet(f"color: {theme.TONES['danger'][0]};")
        self.error.hide()
        layout.addWidget(self.error)

        buttons = QHBoxLayout()
        self.later_btn = button("나중에 하기", "ghost")
        self.later_btn.clicked.connect(self.reject)
        self.back_btn = button("이전")
        self.back_btn.clicked.connect(lambda: self.go(self.step - 1))
        self.next_btn = button("다음", "primary")
        self.next_btn.setMinimumWidth(110)
        self.next_btn.setDefault(True)
        self.next_btn.clicked.connect(self.next)
        buttons.addWidget(self.later_btn)
        buttons.addStretch(1)
        buttons.addWidget(self.back_btn)
        buttons.addWidget(self.next_btn)
        layout.addLayout(buttons)
        self._prefill()
        self.go(STEP_CONNECT)

    # ------------------------------------------------------------------ 화면
    def _connect_page(self) -> QWidget:
        w = QWidget()
        col = QVBoxLayout(w)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(12)
        self.legacy_card = Card("기존 배포 툴 설정을 찾았습니다",
                                "compiled-java-deployer 에 등록한 프로젝트와 GitLab 주소를 그대로 가져올 수 있습니다. "
                                "토큰은 가져오지 않으니 아래에 다시 넣어 주세요.")
        row = QHBoxLayout()
        self.legacy_btn = button("가져오기", "primary")
        self.legacy_btn.clicked.connect(self.import_legacy)
        self.legacy_note = label("", "muted")
        row.addWidget(self.legacy_btn)
        row.addWidget(self.legacy_note, 1)
        self.legacy_card.body.addLayout(row)
        self.legacy_card.setVisible(self.legacy_db is not None)
        col.addWidget(self.legacy_card)

        card = Card("GitLab 연결", "MR 번호만 고르면 바뀐 파일을 알아서 모읍니다. 이를 위해 GitLab 주소와 토큰이 필요합니다.")
        form = _form()
        self.gitlab_url = QLineEdit()
        self.gitlab_url.setPlaceholderText("예: https://gitlab.example.com")
        self.token_edit = QLineEdit()
        self.token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.token_edit.setPlaceholderText("붙여넣기 (화면에 보이지 않음)")
        self.test_btn = button("연결 확인", "ghost")
        self.test_btn.clicked.connect(self.test_connection)
        token_row = QHBoxLayout()
        token_row.setSpacing(8)
        token_row.addWidget(self.token_edit, 1)
        token_row.addWidget(self.test_btn)
        form.addRow(label("GitLab 주소", "fieldLabel", wrap=False), self.gitlab_url)
        form.addRow(label("토큰", "fieldLabel", wrap=False), token_row)
        card.body.addLayout(form)
        self.test_result = label("", "muted")
        card.body.addWidget(self.test_result)
        card.body.addWidget(label("토큰 만들기: GitLab 오른쪽 위 프로필 → Preferences → Access tokens → "
                                  "read_api 권한을 체크해 만듭니다. 토큰은 이 PC 의 Windows 자격 증명 관리자에만 저장됩니다.",
                                  "muted"))
        self.no_gitlab = QCheckBox("GitLab 을 쓰지 않습니다 (git 커밋이나 파일 목록으로만 배포)")
        self.no_gitlab.toggled.connect(self._toggle_gitlab)
        card.body.addWidget(self.no_gitlab)
        col.addWidget(card)
        col.addWidget(label("GitHub 는 나중에 왼쪽 [연결] 메뉴에서 추가할 수 있습니다.", "muted"))
        col.addStretch(1)
        return w

    def _output_page(self) -> QWidget:
        w = QWidget()
        col = QVBoxLayout(w)
        col.setContentsMargins(0, 0, 0, 0)
        card = Card("패키지를 만들 위치", "배포할 때마다 이 폴더 아래에 프로젝트별·날짜별 폴더를 만들고 패키지를 넣습니다. "
                                         "{project} 는 프로젝트 이름으로 바뀝니다.")
        form = _form()
        self.output_field = PathField(placeholder="예: D:/deploy/{project}")
        self.output_field.edit.textChanged.connect(self._show_output_example)
        form.addRow(label("패키지 생성 경로", "fieldLabel", wrap=False), self.output_field)
        card.body.addLayout(form)
        self.output_example = label("", "muted")
        card.body.addWidget(self.output_example)
        col.addWidget(card)
        col.addStretch(1)
        return w

    def _project_page(self) -> QWidget:
        w = QWidget()
        col = QVBoxLayout(w)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(12)
        self.imported_note = label("", "muted")
        self.imported_note.hide()
        col.addWidget(self.imported_note)
        card = Card("배포할 프로젝트", "로컬에 받아 둔 git 저장소 폴더를 고르면 나머지는 최대한 알아서 채웁니다. "
                                      "반영 규칙 같은 자세한 설정은 나중에 [프로젝트] 메뉴에서 바꿉니다.")
        form = _form()
        self.root_field = PathField(placeholder="로컬 git 저장소 최상위 폴더")
        self.root_field.edit.textChanged.connect(self._inspect_root)
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("폴더를 고르면 폴더 이름으로 채웁니다")
        self.name_edit.textEdited.connect(lambda *_: setattr(self, "_name_edited", True))
        self.preset_combo = QComboBox()
        for key, preset in PRESETS.items():
            self.preset_combo.addItem(f"{key}  -  {preset['description']}", key)
        self.server_edit = QLineEdit()
        self.server_edit.setPlaceholderText("서버에서 이 프로젝트가 있는 폴더. 예: /app/tomcat/webapps/ROOT")
        self.gitlab_project = QLineEdit()
        self.gitlab_project.setPlaceholderText("GitLab 프로젝트 ID 또는 그룹/프로젝트 (예: erp/web)")
        for text, widget in (("로컬 폴더", self.root_field), ("이름", self.name_edit), ("프리셋", self.preset_combo),
                             ("서버 배포 경로", self.server_edit), ("GitLab 프로젝트", self.gitlab_project)):
            form.addRow(label(text, "fieldLabel", wrap=False), widget)
        card.body.addLayout(form)
        self.root_note = label("", "muted")
        card.body.addWidget(self.root_note)
        col.addWidget(card)
        col.addStretch(1)
        return w

    # ------------------------------------------------------------------ 값 채우기
    def _prefill(self) -> None:
        try:
            data = config_store.load_raw(self.ctx.config_path)
        except Exception:
            data = {}
        self.gitlab_url.setText(str((data.get("gitlab") or {}).get("url", "")))
        output = (data.get("defaults") or {}).get("output_dir")
        self.output_field.setText(str(output or suggest_output_dir()))

    def _toggle_gitlab(self, off: bool) -> None:
        for widget in (self.gitlab_url, self.token_edit, self.test_btn, self.gitlab_project):
            widget.setEnabled(not off)

    def _show_output_example(self) -> None:
        text = self.output_field.text() or "D:/deploy/{project}"
        self.output_example.setText("예: " + text.replace("{project}", "erp").rstrip("/") + "/2026-10-29-1140")

    def _inspect_root(self) -> None:
        """로컬 폴더를 고르면 이름·프리셋·서버 경로·GitLab 프로젝트를 추정한다."""
        raw = self.root_field.text()
        root = Path(raw) if raw else None
        if not root or not root.is_dir():
            self.root_note.setText("폴더가 없습니다." if raw else "")
            return
        top = find_repo_root(root)
        if top != root:
            self.root_field.setText(top.as_posix())  # 하위 폴더를 고르면 저장소 최상위로
            return
        if not self._name_edited:
            self.name_edit.setText(root.name)
        notes = []
        shared_file = root / PROJECT_FILE
        shared: Dict[str, Any] = {}
        if shared_file.is_file():
            try:
                shared = load_project_file(shared_file)
            except ConfigError as e:
                notes.append(f"{PROJECT_FILE} 오류: {e}")
        if shared.get("preset"):
            preset, why = str(shared["preset"]), PROJECT_FILE
        else:
            preset, why = detect_preset(root)
        idx = self.preset_combo.findData(preset)
        if idx >= 0:
            self.preset_combo.setCurrentIndex(idx)
        notes.append(f"프리셋은 {why} 를 보고 {preset} 으로 골랐습니다.")
        if shared.get("server_path") and not self.server_edit.text().strip():
            self.server_edit.setText(str(shared["server_path"]))
            notes.append(f"서버 배포 경로는 {PROJECT_FILE} 값을 썼습니다.")
        if not self.gitlab_project.text().strip():
            found = str(shared.get("gitlab_project") or "") or (
                git_local.remote_path(root, self.gitlab_url.text()) if self.gitlab_url.text().strip() else None)
            if found:
                self.gitlab_project.setText(found)
                notes.append("GitLab 프로젝트는 저장소의 원격 주소로 채웠습니다.")
        if not git_local.is_git_repo(root):
            notes.append("git 저장소가 아닙니다. MR·커밋으로 배포하려면 git 으로 받은 폴더를 고르세요.")
        self.root_note.setText("  ".join(notes))

    # ------------------------------------------------------------------ 이동
    def go(self, step: int) -> None:
        step = max(STEP_CONNECT, min(STEP_PROJECT, step))
        self.step = step
        self.pages.setCurrentIndex(step)
        self.stepper.set_state(step, step)
        self.back_btn.setVisible(step > STEP_CONNECT)
        self.next_btn.setText("완료" if step == STEP_PROJECT else "다음")
        self.error.hide()

    def next(self) -> None:
        problem = self.check(self.step)
        if problem:
            self.error.setText(problem)
            self.error.show()
            return
        if self.step == STEP_PROJECT:
            self.finish()
        else:
            self.go(self.step + 1)

    def check(self, step: int) -> Optional[str]:
        """그 단계에서 다음으로 넘어갈 수 없는 이유 (없으면 None)."""
        if step == STEP_CONNECT and not self.no_gitlab.isChecked():
            url = self.gitlab_url.text().strip()
            if not url:
                return "GitLab 주소를 입력하세요. GitLab 을 쓰지 않으면 아래 체크 상자를 켜세요."
            if not url.startswith(("http://", "https://")):
                return "GitLab 주소는 http:// 또는 https:// 로 시작해야 합니다."
        if step == STEP_OUTPUT and not self.output_field.text():
            return "패키지를 만들 폴더를 정하세요."
        if step == STEP_PROJECT:
            root = self.root_field.text()
            if not root and self.imported:
                return None  # 가져온 프로젝트로 충분
            if not root:
                return "배포할 프로젝트의 로컬 폴더를 고르세요."
            if not Path(root).is_dir():
                return f"폴더가 없습니다: {root}"
            if not self.name_edit.text().strip():
                return "프로젝트 이름을 입력하세요."
            if not self.server_edit.text().strip():
                return "서버 배포 경로를 입력하세요. 서버에서 이 프로젝트 파일이 있는 폴더입니다."
        return None

    # ------------------------------------------------------------------ 연결 확인·가져오기
    def _gitlab_config(self) -> GitLabConfig:
        return GitLabConfig(url=self.gitlab_url.text().strip().rstrip("/"))

    def test_connection(self) -> None:
        url = self.gitlab_url.text().strip()
        token = self.token_edit.text().strip()
        if not url or not token:
            self.test_result.setText("주소와 토큰을 넣은 뒤 확인하세요.")
            return
        cfg = self._gitlab_config()
        self.test_result.setText("확인 중…")

        def run():
            user = GitLabClient(cfg, token).current_user()
            return f"연결됨: {user.get('username')} ({user.get('name')})"

        def done(msg: str) -> None:
            self.test_result.setText(f"<span style='color:{theme.TONES['success'][0]}'>✓ {msg}</span>")

        def failed(msg: str) -> None:
            self.test_result.setText(f"<span style='color:{theme.TONES['danger'][0]}'>연결하지 못했습니다: {msg}</span>")

        self.ctx.runner.run(run, done, failed)

    def import_legacy(self) -> None:
        if not self.legacy_db:
            return
        data: Dict[str, Any] = {}
        try:
            added, _ = config_store.merge_legacy(data, self.legacy_db)
        except Exception as e:
            self.legacy_note.setText(f"가져오지 못했습니다: {e}")
            return
        self.legacy_data = data
        self.imported = added
        url = (data.get("gitlab") or {}).get("url")
        if url:
            self.gitlab_url.setText(str(url))
        output = (data.get("defaults") or {}).get("output_dir")
        if output:
            self.output_field.setText(str(output))
        self.legacy_btn.setEnabled(False)
        self.legacy_btn.setText("가져옴")
        self.legacy_note.setText(f"프로젝트 {len(added)}개를 가져옵니다: {', '.join(added)}  ([완료]를 누르면 저장)")
        self.imported_note.setText(f"기존 툴에서 프로젝트 {len(added)}개를 가져왔습니다 ({', '.join(added)}). "
                                   "다른 프로젝트를 더 등록하지 않으려면 비워 두고 [완료]를 누르세요.")
        self.imported_note.show()

    # ------------------------------------------------------------------ 완료
    def build_config(self) -> Dict[str, Any]:
        data = config_store.load_raw(self.ctx.config_path)
        if self.legacy_data:
            config_store.merge_legacy(data, self.legacy_db)
        if not self.no_gitlab.isChecked():
            data["gitlab"] = {**(data.get("gitlab") or {}), "url": self.gitlab_url.text().strip()}
        defaults = data.setdefault("defaults", {}) or {}
        data["defaults"] = defaults
        defaults["output_dir"] = self.output_field.text()
        if self.root_field.text():
            config_store.set_project(data, self.name_edit.text().strip(), {
                "root": self.root_field.text(),
                "preset": self.preset_combo.currentData(),
                "server_path": self.server_edit.text().strip(),
                "gitlab_project": "" if self.no_gitlab.isChecked() else self.gitlab_project.text().strip(),
            })
        return data

    def finish(self) -> bool:
        try:
            data = self.build_config()
            cfg = config_store.save_raw(self.ctx.config_path, data)
        except ConfigError as e:
            self.error.setText(f"저장하지 못했습니다: {e}")
            self.error.show()
            return False
        token = self.token_edit.text().strip()
        if token and cfg.gitlab and not self.no_gitlab.isChecked():
            try:
                set_token(cfg.gitlab, token)
            except SecretError as e:
                self.ctx.dialogs.error("토큰 저장 실패", f"{e}\n설정은 저장했습니다. [연결] 메뉴에서 토큰을 다시 저장하세요.")
        self.selected = (self.name_edit.text().strip() if self.root_field.text() else None) or \
            (self.imported[0] if self.imported else None)
        self.accept()
        return True
