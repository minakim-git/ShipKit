"""ShipKit 메인 창: 왼쪽(로고 · 작업할 프로젝트 · 메뉴 · 연결 상태) + 오른쪽 화면."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional, Sequence

from PySide6.QtCore import QCoreApplication, QSize, Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMainWindow, QStackedWidget,
    QVBoxLayout, QWidget,
)

from shipkit import __app_name__, __tagline__, __version__, legacy
from shipkit.config import ConfigError, find_config, user_config_path
from shipkit.gui import theme
from shipkit.gui.connections_page import ConnectionsPage
from shipkit.gui.context import AppContext
from shipkit.gui.deploy_page import DeployPage
from shipkit.gui.history_page import HistoryPage
from shipkit.gui.projects_page import ProjectsPage
from shipkit.gui.settings_page import SettingsPage
from shipkit.gui.setup_wizard import SetupWizard
from shipkit.gui.tasks import TaskRunner
from shipkit.gui.widgets import Dialogs, ElidedComboBox, label
from shipkit.secrets import token_source

PAGES = (("배포", "nav-deploy.svg"), ("기록", "nav-history.svg"), ("프로젝트", "nav-projects.svg"),
         ("연결", "nav-connections.svg"), ("앱 설정", "nav-settings.svg"))
PAGE_DEPLOY, PAGE_HISTORY, PAGE_PROJECTS, PAGE_CONNECTIONS, PAGE_SETTINGS = range(5)
ADD_PROJECT, IMPORT_LEGACY = "__add__", "__legacy__"  # 프로젝트 목록 맨 아래의 동작 항목


def default_config_path() -> Path:
    try:
        return find_config(None)
    except ConfigError:
        return user_config_path()


class MainWindow(QMainWindow):
    def __init__(self, config_path: Optional[Path] = None, runner=None, dialogs: Optional[Dialogs] = None):
        super().__init__()
        self.setWindowTitle(f"{__app_name__} - {__tagline__}")
        self.setWindowIcon(QIcon(theme.logo_pixmap(32)))
        self.resize(1240, 840)
        self.setMinimumSize(1000, 660)
        self.ctx = AppContext(config_path=config_path or default_config_path(),
                              runner=runner or TaskRunner(self), dialogs=dialogs or Dialogs(self))

        self.stack = QStackedWidget()
        self.deploy_page = DeployPage(self.ctx)
        self.history_page = HistoryPage(self.ctx)
        self.projects_page = ProjectsPage(self.ctx)
        self.connections_page = ConnectionsPage(self.ctx)
        self.settings_page = SettingsPage(self.ctx)
        self.wizard_factory = SetupWizard  # 테스트에서 바꿔 끼운다
        self.history_page.remake_requested.connect(self.remake)
        self.settings_page.setup_requested.connect(self.run_setup)
        self.settings_page.import_requested.connect(self._import_legacy)
        for page in (self.deploy_page, self.history_page, self.projects_page, self.connections_page,
                     self.settings_page):
            self.stack.addWidget(page)

        central = QWidget()
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._sidebar())
        page_area = QWidget()
        page_area.setObjectName("pageArea")
        page_area.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        page_layout = QVBoxLayout(page_area)
        page_layout.setContentsMargins(28, 22, 28, 20)
        page_layout.addWidget(self.stack)
        layout.addWidget(page_area, 1)
        self.setCentralWidget(central)

        self.ctx.listeners.append(self._refresh_projects)
        self.ctx.listeners.append(self._refresh_status)
        self.ctx.connection_listeners.append(self._refresh_status)
        self.ctx.project_listeners.append(self._show_project)
        self.ctx.reload()
        self.nav.setCurrentRow(PAGE_DEPLOY if self.ctx.project else (PAGE_PROJECTS if self.ctx.cfg else PAGE_CONNECTIONS))

    # ------------------------------------------------------------------ 왼쪽
    def _sidebar(self) -> QWidget:
        sidebar = QWidget()
        sidebar.setObjectName("sidebar")
        sidebar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        sidebar.setFixedWidth(236)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(0, 22, 0, 16)
        side.setSpacing(0)

        brand_row = QHBoxLayout()
        brand_row.setContentsMargins(22, 0, 16, 0)
        brand_row.setSpacing(10)
        logo = QLabel()
        logo.setPixmap(theme.logo_pixmap(34))
        texts = QVBoxLayout()
        texts.setSpacing(0)
        texts.addWidget(label(__app_name__, "brand", wrap=False))
        texts.addWidget(label(f"v{__version__}", "brandSub", wrap=False))
        brand_row.addWidget(logo)
        brand_row.addLayout(texts, 1)
        side.addLayout(brand_row)
        side.addSpacing(24)

        # 작업할 프로젝트: 고르면 모든 화면이 이 프로젝트 기준으로 바뀐다
        box = QVBoxLayout()
        box.setContentsMargins(16, 0, 16, 0)
        box.setSpacing(5)
        box.addWidget(label("작업할 프로젝트", "sideCaption", wrap=False))
        self.project_switcher = ElidedComboBox()
        self.project_switcher.setObjectName("projectSwitcher")
        self.project_switcher.setCursor(Qt.CursorShape.PointingHandCursor)
        self.project_switcher.activated.connect(self._on_switch)
        box.addWidget(self.project_switcher)
        self.project_meta = label("", "sideMeta", wrap=False)
        box.addWidget(self.project_meta)
        side.addLayout(box)
        side.addSpacing(18)

        self.nav = QListWidget()
        self.nav.setObjectName("nav")
        self.nav.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.nav.setIconSize(QSize(18, 18))
        for text, icon in PAGES:
            self.nav.addItem(QListWidgetItem(theme.svg_icon(icon, theme.MUTED, 18, theme.PRIMARY), f"  {text}"))
        self.nav.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.nav.currentRowChanged.connect(self._on_page)
        side.addWidget(self.nav, 1)

        # 연결 상태 (연결 화면에서 바꾼 내용이 바로 반영된다)
        self.status_box = QVBoxLayout()
        self.status_box.setContentsMargins(22, 0, 16, 0)
        self.status_box.setSpacing(4)
        side.addLayout(self.status_box)
        return sidebar

    def _refresh_projects(self) -> None:
        """설정을 다시 읽었을 때: 프로젝트 목록을 새로 채운다."""
        sw = self.project_switcher
        sw.blockSignals(True)
        sw.clear()
        for name in self.ctx.project_names():
            sw.addItem(name, name)
        if sw.count():
            sw.insertSeparator(sw.count())
        sw.addItem("+  새 프로젝트 추가", ADD_PROJECT)
        sw.addItem("기존 툴에서 가져오기", IMPORT_LEGACY)
        sw.blockSignals(False)

    def _refresh_status(self) -> None:
        """왼쪽 아래 연결 상태 (설정 파일을 다시 읽거나 토큰을 저장·삭제했을 때)."""
        while self.status_box.count():
            item = self.status_box.takeAt(0)
            if item.widget():
                item.widget().hide()
                item.widget().deleteLater()
        cfg = self.ctx.cfg
        if self.ctx.load_error:
            lines = [("danger", "설정 파일 오류", self.ctx.load_error)]
        else:
            lines = []
            for key, name in (("gitlab", "GitLab"), ("github", "GitHub")):
                service = getattr(cfg, key, None) if cfg else None
                if service is None:
                    continue
                try:
                    has = token_source(service) is not None
                except Exception:
                    has = False
                tone = "success" if has else ("danger" if key == "gitlab" else "warning")
                lines.append((tone, f"{name}  {'연결됨' if has else '토큰 없음'}", service.url))
        for tone, text, tip in lines:
            w = label(f"<span style='color:{theme.TONES[tone][0]}'>●</span>&nbsp;&nbsp;{text}", "sideStatus", wrap=False)
            w.setToolTip(tip)
            self.status_box.addWidget(w)
        path = label(str(self.ctx.config_path.name), "sideFooter", wrap=False)
        path.setToolTip(f"설정 파일: {self.ctx.config_path}")
        self.status_box.addSpacing(4)
        self.status_box.addWidget(path)

    def _show_project(self) -> None:
        """작업 프로젝트가 바뀌었을 때: 전환기 선택과 아래 설명을 맞춘다."""
        p = self.ctx.project
        index = self.project_switcher.findData(self.ctx.current) if p else -1
        self.project_switcher.setCurrentIndex(index)
        self.project_switcher.setToolTip(self.ctx.current or "")
        if index < 0:
            self.project_switcher.setPlaceholderText("프로젝트 없음")
        server = "Windows 서버" if p and p.server_os == "windows" else "리눅스 서버"
        self.project_meta.setText(f"{p.preset or ('sync' if p.mode == 'sync' else '직접 규칙')}  ·  {server}" if p else "")

    def _on_switch(self, index: int) -> None:
        data = self.project_switcher.itemData(index)
        if data in (ADD_PROJECT, IMPORT_LEGACY):
            self._show_project()  # 동작 항목은 선택으로 남기지 않는다
            if data == ADD_PROJECT:
                self.go(PAGE_PROJECTS)
                self.projects_page.add_project()
            else:
                self._import_legacy()
            return
        self.ctx.set_project(data)

    # ------------------------------------------------------------------ 이동
    def _on_page(self, page: int) -> None:
        if page == PAGE_HISTORY:  # 배포 화면에서 방금 만든 패키지가 바로 보이게
            self.history_page.refresh()

    def go(self, page: int) -> None:
        self.nav.setCurrentRow(page)

    def remake(self, record) -> None:
        """기록의 [같은 내용으로 다시 만들기]: 배포 화면 ① 고르기에 같은 MR·파일을 채운다."""
        self.deploy_page.prefill(record)
        self.go(PAGE_DEPLOY)

    def _import_legacy(self) -> None:
        self.go(PAGE_PROJECTS)
        self.projects_page.import_legacy()

    def first_run(self) -> None:
        """설정 파일이 없으면 처음 실행 안내를 띄운다. 설정 파일이 잘못됐으면 알려 준다."""
        if self.ctx.config_path.is_file():
            if self.ctx.load_error:
                self.ctx.dialogs.error("설정 파일 오류",
                                       f"{self.ctx.config_path}\n\n{self.ctx.load_error}\n\n설정 파일을 고친 뒤 다시 실행하세요.")
            return
        self.run_setup()

    def run_setup(self) -> None:
        """처음 실행 안내 (앱 설정에서 다시 열 수도 있다). 끝나면 고른 프로젝트로 배포 화면을 연다."""
        wizard = self.wizard_factory(self.ctx, self, legacy.legacy_db_path())
        if wizard.exec():
            self.ctx.reload(select=wizard.selected)
            self.ctx.connections_changed()
            self.go(PAGE_DEPLOY if self.ctx.project else PAGE_PROJECTS)
        elif not self.ctx.config_path.is_file():
            self.go(PAGE_CONNECTIONS)


def main(argv: Optional[Sequence[str]] = None) -> int:
    QCoreApplication.setOrganizationName(__app_name__)
    QCoreApplication.setApplicationName(__app_name__)
    app = QApplication.instance() or QApplication(list(argv) if argv is not None else sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(theme.stylesheet())
    window = MainWindow()
    window.show()
    window.first_run()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
