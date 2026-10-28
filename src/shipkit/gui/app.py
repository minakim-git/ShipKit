"""ShipKit 메인 창: 왼쪽 메뉴(배포 / 프로젝트 / 설정) + 오른쪽 화면."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional, Sequence

from PySide6.QtCore import QCoreApplication, QSize, Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMainWindow, QStackedWidget, QVBoxLayout,
    QWidget,
)

from shipkit import __app_name__, __tagline__, __version__, legacy
from shipkit.config import ConfigError, find_config, user_config_path
from shipkit.gui import theme
from shipkit.gui.context import AppContext
from shipkit.gui.deploy_page import DeployPage
from shipkit.gui.projects_page import ProjectsPage
from shipkit.gui.settings_page import SettingsPage
from shipkit.gui.tasks import TaskRunner
from shipkit.gui.widgets import Dialogs, label

PAGES = (("배포", "nav-deploy.svg"), ("프로젝트", "nav-projects.svg"), ("설정", "nav-settings.svg"))
PAGE_DEPLOY, PAGE_PROJECTS, PAGE_SETTINGS = range(3)


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
        self.resize(1220, 820)
        self.setMinimumSize(980, 640)
        self.ctx = AppContext(config_path=config_path or default_config_path(),
                              runner=runner or TaskRunner(self), dialogs=dialogs or Dialogs(self))

        self.stack = QStackedWidget()
        self.deploy_page = DeployPage(self.ctx)
        self.projects_page = ProjectsPage(self.ctx)
        self.settings_page = SettingsPage(self.ctx)
        for page in (self.deploy_page, self.projects_page, self.settings_page):
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
        page_layout.setContentsMargins(28, 22, 28, 18)
        page_layout.addWidget(self.stack)
        layout.addWidget(page_area, 1)
        self.setCentralWidget(central)
        self.statusBar().setSizeGripEnabled(False)

        self.ctx.listeners.append(self._update_status)
        self.ctx.reload()
        self.nav.setCurrentRow(PAGE_DEPLOY if self.ctx.cfg and self.ctx.cfg.projects else PAGE_SETTINGS)

    def _sidebar(self) -> QWidget:
        sidebar = QWidget()
        sidebar.setObjectName("sidebar")
        sidebar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        sidebar.setFixedWidth(220)
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
        side.addSpacing(22)

        self.nav = QListWidget()
        self.nav.setObjectName("nav")
        self.nav.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.nav.setIconSize(QSize(18, 18))
        for text, icon in PAGES:
            item = QListWidgetItem(theme.svg_icon(icon, theme.MUTED, 18, theme.PRIMARY), f"  {text}")
            self.nav.addItem(item)
        self.nav.currentRowChanged.connect(self.stack.setCurrentIndex)
        side.addWidget(self.nav, 1)

        footer = label(__tagline__, "sideFooter")
        footer.setContentsMargins(22, 0, 16, 0)
        side.addWidget(footer)
        return sidebar

    def _update_status(self) -> None:
        cfg = self.ctx.cfg
        if self.ctx.load_error:
            self.statusBar().showMessage(f"설정 파일 오류: {self.ctx.load_error}")
        elif cfg is None:
            self.statusBar().showMessage("설정 파일이 없습니다. 설정 화면에서 저장하거나 기존 툴에서 가져오세요.")
        else:
            self.statusBar().showMessage(f"설정 파일: {self.ctx.config_path}  ·  프로젝트 {len(cfg.projects)}개")

    def go(self, page: int) -> None:
        self.nav.setCurrentRow(page)

    def first_run(self) -> None:
        """설정 파일이 없을 때: 기존 툴 설정 가져오기를 권하고, 아니면 설정 화면으로 안내한다."""
        if self.ctx.config_path.is_file():
            if self.ctx.load_error:
                self.ctx.dialogs.error("설정 파일 오류",
                                       f"{self.ctx.config_path}\n\n{self.ctx.load_error}\n\n설정 파일을 고친 뒤 다시 실행하세요.")
            return
        db = legacy.legacy_db_path()
        if db.is_file() and self.ctx.dialogs.ask(
                "처음 실행", f"기존 배포 툴(compiled-java-deployer)의 프로젝트 설정을 찾았습니다.\n가져올까요?\n\n{db}"):
            self.projects_page.import_legacy()
            self.go(PAGE_PROJECTS)
            return
        self.ctx.dialogs.info("처음 실행", "GitLab/GitHub 주소와 패키지 생성 경로를 입력하고 '설정 저장'을 누른 뒤,\n"
                                         "프로젝트 화면에서 프로젝트를 추가하세요.")
        self.go(PAGE_SETTINGS)


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
