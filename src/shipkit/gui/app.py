"""ShipKit 메인 창: 왼쪽(로고 · 작업할 프로젝트 · 메뉴 · 연결 상태) + 오른쪽 화면."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional, Sequence

from PySide6.QtCore import QCoreApplication, QEvent, QSettings, QSize, Qt
from PySide6.QtGui import QGuiApplication, QIcon, QKeySequence, QShortcut
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMainWindow, QStackedWidget, QSystemTrayIcon,
    QVBoxLayout, QWidget,
)

from shipkit import __app_name__, __tagline__, __version__, legacy, update
from shipkit.config import ConfigError, find_config, user_config_path
from shipkit.gui import theme
from shipkit.gui.connections_page import ConnectionsPage
from shipkit.gui.context import AppContext
from shipkit.gui import diagnostics
from shipkit.gui.deploy_page import DeployPage
from shipkit.gui.guide_view import show_guide
from shipkit.gui.history_page import HistoryPage
from shipkit.gui.projects_page import TAB_SERVERS, ProjectsPage
from shipkit.gui.server_page import ServerPage
from shipkit.gui.settings_page import SettingsPage
from shipkit.gui.setup_wizard import SetupWizard
from shipkit.gui.tasks import TaskRunner
from shipkit.gui.widgets import Dialogs, ProjectSwitcher, button, label, open_location
from shipkit.secrets import token_source

PAGES = (("배포", "nav-deploy.svg"), ("기록", "nav-history.svg"), ("서버", "nav-server.svg"),
         ("프로젝트", "nav-projects.svg"), ("연결", "nav-connections.svg"), ("앱 설정", "nav-settings.svg"))
PAGE_DEPLOY, PAGE_HISTORY, PAGE_SERVER, PAGE_PROJECTS, PAGE_CONNECTIONS, PAGE_SETTINGS = range(6)
ADD_PROJECT, IMPORT_LEGACY = "__add__", "__legacy__"  # 프로젝트 목록 맨 아래의 동작 항목


def current_theme_setting() -> str:
    """앱 설정의 화면 테마 (system 또는 테마 키. 예전 값 light/dark 도 받는다)."""
    value = str(QSettings().value("ui/theme", "system"))
    return value if value in dict(theme.SETTINGS) or value in theme.ALIASES else "system"


def default_config_path() -> Path:
    try:
        return find_config(None)
    except ConfigError:
        return user_config_path()


class MainWindow(QMainWindow):
    def __init__(self, config_path: Optional[Path] = None, runner=None, dialogs: Optional[Dialogs] = None):
        super().__init__()
        self.setWindowTitle(f"{__app_name__} - {__tagline__}")
        self.setWindowIcon(QIcon(theme.logo_pixmap(32, mode="light")))  # 작업 표시줄은 앱 테마와 무관
        self.resize(1320, 860)
        self.setMinimumSize(1000, 660)   # 실제 최소 폭은 화면 내용에 맞춰 더 커질 수 있다 (_fit_minimum)
        self.ctx = AppContext(config_path=config_path or default_config_path(),
                              runner=runner or TaskRunner(self), dialogs=dialogs or Dialogs(self))

        self.stack = QStackedWidget()
        self.deploy_page = DeployPage(self.ctx)
        self.history_page = HistoryPage(self.ctx)
        self.server_page = ServerPage(self.ctx)
        self.projects_page = ProjectsPage(self.ctx)
        self.connections_page = ConnectionsPage(self.ctx)
        self.settings_page = SettingsPage(self.ctx)
        self.wizard_factory = SetupWizard  # 테스트에서 바꿔 끼운다
        self.history_page.remake_requested.connect(self.remake)
        self.deploy_page.remote_panel.settings_requested.connect(self._open_server_settings)
        self.settings_page.setup_requested.connect(self.run_setup)
        self.settings_page.import_requested.connect(self._import_legacy)
        self.deploy_page.add_project_requested.connect(self.add_project)
        self.history_page.deploy_requested.connect(lambda: self.go(PAGE_DEPLOY))
        self.history_page.browse_requested.connect(self.browse_server)
        self.server_page.settings_requested.connect(self._open_server_settings)
        for i in range(len(PAGES)):   # Ctrl+1 ~ Ctrl+6: 메뉴 이동
            QShortcut(QKeySequence(f"Ctrl+{i + 1}"), self, activated=lambda page=i: self.go(page))
        for page in (self.deploy_page, self.history_page, self.server_page, self.projects_page,
                     self.connections_page, self.settings_page):
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
        page_layout.setContentsMargins(24, 20, 24, 18)
        page_layout.addWidget(self.stack)
        layout.addWidget(page_area, 1)
        self.setCentralWidget(central)

        self.ctx.notify = self.notify
        self.tray: Optional[QSystemTrayIcon] = None
        self.ctx.listeners.append(self._refresh_projects)
        self.ctx.listeners.append(self._refresh_status)
        self.ctx.listeners.append(self._check_update)
        self.ctx.connection_listeners.append(self._refresh_status)
        theme.on_change(self._refresh_status)
        self.ctx.project_listeners.append(self._show_project)
        self.ctx.reload()
        self.nav.setCurrentRow(PAGE_DEPLOY if self.ctx.project else (PAGE_PROJECTS if self.ctx.cfg else PAGE_CONNECTIONS))
        geometry = QSettings().value("ui/geometry")   # 지난번 창 크기·위치
        if geometry is not None:
            self.restoreGeometry(geometry)

    def event(self, e) -> bool:  # noqa: N802
        if e.type() in (QEvent.Type.LayoutRequest, QEvent.Type.Show):   # 글자 크기·단계·사이드바 접기 등으로 내용이 바뀌면
            QTimer.singleShot(0, self._fit_minimum)                      # 자식들의 크기 힌트가 갱신된 뒤에 잰다
        return super().event(e)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._fit_minimum()

    def _fit_minimum(self) -> None:
        """창 최소 폭 = max(1000, 지금 보이는 화면의 최소 폭 + 사이드바·여백). 내용보다 좁게 줄이면 단추 글자가 잘리므로.
        화면을 바꾸면 다시 재고, 모니터 폭은 넘지 않는다."""
        page = self.stack.currentWidget()
        if page is None or not self.isVisible() or self.stack.width() <= 0:
            return
        width = max(1000, page.minimumSizeHint().width() + self.width() - self.stack.width())
        screen = self.screen()
        if screen is not None and screen.availableGeometry().width() >= 1000:   # 모니터보다 커지지는 않게
            width = min(width, screen.availableGeometry().width() - 40)
        if width != self.minimumWidth():
            self.setMinimumSize(width, 660)

    def closeEvent(self, event) -> None:
        QSettings().setValue("ui/geometry", self.saveGeometry())
        self.server_page.disconnect()
        super().closeEvent(event)

    def add_project(self) -> None:
        """프로젝트 화면으로 가서 새 프로젝트 추가 (전환기의 [+ 새 프로젝트 추가], 빈 배포 화면의 버튼)."""
        self.go(PAGE_PROJECTS)
        self.projects_page.add_project()

    # ------------------------------------------------------------------ 왼쪽
    def _sidebar(self) -> QWidget:
        sidebar = QWidget()
        sidebar.setObjectName("sidebar")
        sidebar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        sidebar.setFixedWidth(224)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(0, 22, 0, 16)
        side.setSpacing(0)

        brand_row = QHBoxLayout()
        brand_row.setContentsMargins(18, 0, 16, 0)  # 아래 프로젝트 상자(16px) 와 같은 선에 맞춤
        brand_row.setSpacing(10)
        logo = QLabel()
        theme.bind_logo(logo, 30, frame=False)  # 사이드바에서는 네모 없이 공구 선만, 테마 색으로
        self.brand_texts = QWidget()
        texts = QVBoxLayout(self.brand_texts)
        texts.setContentsMargins(0, 0, 0, 0)
        texts.setSpacing(0)
        texts.addWidget(label(__app_name__, "brand", wrap=False))
        texts.addWidget(label(f"v{__version__}", "brandSub", wrap=False))
        brand_row.addWidget(logo)
        brand_row.addWidget(self.brand_texts, 1)
        side.addLayout(brand_row)
        side.addSpacing(20)

        # 작업할 프로젝트: 고르면 모든 화면이 이 프로젝트 기준으로 바뀐다
        self.project_box = QWidget()
        box = QVBoxLayout(self.project_box)
        box.setContentsMargins(16, 0, 16, 0)
        box.setSpacing(5)
        box.addWidget(label("작업할 프로젝트", "sideCaption", wrap=False))
        self.project_switcher = ProjectSwitcher()
        self.project_switcher.setObjectName("projectSwitcher")
        self.project_switcher.setCursor(Qt.CursorShape.PointingHandCursor)
        self.project_switcher.activated.connect(self._on_switch)
        box.addWidget(self.project_switcher)
        side.addWidget(self.project_box)
        side.addSpacing(18)

        self.nav = QListWidget()
        self.nav.setObjectName("nav")
        self.nav.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.nav.setIconSize(QSize(18, 18))
        self.nav.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)  # 접었을 때 가로 스크롤이 생기지 않게
        for text, _icon in PAGES:
            self.nav.addItem(QListWidgetItem(f"  {text}"))
        self._nav_icons()
        theme.on_change(self._nav_icons)
        self.nav.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.stack.currentChanged.connect(lambda _i: self._fit_minimum())
        self.nav.currentRowChanged.connect(self._on_page)
        side.addWidget(self.nav, 1)

        # 사용자 가이드: 화면에는 설명글을 두지 않고, 궁금하면 여기서 연다
        self.guide_btn = button("사용자 가이드", "sideLink")
        theme.bind_icon(self.guide_btn, "help.svg", "MUTED", 16)
        self.guide_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.guide_btn.clicked.connect(lambda: show_guide(self))
        guide_row = QHBoxLayout()
        guide_row.setContentsMargins(16, 0, 16, 10)
        guide_row.addWidget(self.guide_btn)
        guide_row.addStretch(1)
        side.addLayout(guide_row)

        # 새 버전 알림: 앱 설정의 확인 위치(공유 폴더·URL)에 더 새 version.txt 가 있을 때만 보인다
        self.update_btn = button("", "sideUpdate")
        theme.bind_icon(self.update_btn, "update.svg", "PRIMARY", 16)
        self.update_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.update_btn.clicked.connect(self._open_update)
        self.update_btn.hide()
        self.update_info: Optional[update.UpdateInfo] = None
        self._update_checked_for = ""
        update_row = QHBoxLayout()
        update_row.setContentsMargins(16, 0, 16, 10)
        update_row.addWidget(self.update_btn)
        side.addLayout(update_row)

        # 연결 상태 (연결 화면에서 바꾼 내용이 바로 반영된다)
        self.status_widget = QWidget()
        self.status_box = QVBoxLayout(self.status_widget)
        self.status_box.setContentsMargins(22, 0, 16, 0)
        self.status_box.setSpacing(4)
        side.addWidget(self.status_widget)

        # 사이드바 접기 (아이콘만 남긴다). 이 PC 에 기억
        self.collapse_btn = button("접기", "sideLink")
        theme.bind_icon(self.collapse_btn, "chevron-left.svg", "MUTED", 16)
        self.collapse_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.collapse_btn.clicked.connect(lambda: self.set_sidebar_collapsed(not self.sidebar_collapsed))
        collapse_row = QHBoxLayout()
        collapse_row.setContentsMargins(16, 10, 16, 0)
        collapse_row.addWidget(self.collapse_btn)
        collapse_row.addStretch(1)
        side.addLayout(collapse_row)
        self.sidebar = sidebar
        self.sidebar_collapsed = False
        if str(QSettings().value("ui/sidebar_collapsed", "false")).lower() == "true":
            self.set_sidebar_collapsed(True, remember=False)
        return sidebar

    def set_sidebar_collapsed(self, collapsed: bool, remember: bool = True) -> None:
        """사이드바를 아이콘만 남기고 접거나 다시 편다."""
        self.sidebar_collapsed = collapsed
        self.sidebar.setFixedWidth(72 if collapsed else 224)
        for w in (self.brand_texts, self.project_box, self.status_widget):
            w.setVisible(not collapsed)
        for i, (text, _icon) in enumerate(PAGES):
            item = self.nav.item(i)
            item.setText("" if collapsed else f"  {text}")
            item.setToolTip(text if collapsed else "")
        self.guide_btn.setText("" if collapsed else "사용자 가이드")
        self.guide_btn.setToolTip("사용자 가이드" if collapsed else "")
        if self.update_info is not None:
            self.update_btn.setText("" if collapsed else f"새 버전 v{self.update_info.version}")
        self.collapse_btn.setText("" if collapsed else "접기")
        self.collapse_btn.setToolTip("사이드바 펴기" if collapsed else "사이드바 접기")
        theme.bind_icon(self.collapse_btn, "chevron-right.svg" if collapsed else "chevron-left.svg", "MUTED", 16)
        if remember:
            QSettings().setValue("ui/sidebar_collapsed", "true" if collapsed else "false")

    def notify(self, title: str, text: str) -> None:
        """Windows 알림 (트레이 풍선). 트레이를 쓸 수 없는 환경이면 조용히 넘어간다."""
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        if self.tray is None:
            self.tray = QSystemTrayIcon(QIcon(theme.logo_pixmap(32, mode="light")), self)
            self.tray.setToolTip(__app_name__)
            self.tray.activated.connect(lambda *_: (self.showNormal(), self.raise_(), self.activateWindow()))
        self.tray.show()
        self.tray.showMessage(title, text, QSystemTrayIcon.MessageIcon.Information, 6000)
        QTimer.singleShot(15000, lambda: self.tray is not None and self.tray.hide())

    # ------------------------------------------------------------------ 새 버전
    def _check_update(self) -> None:
        """설정을 읽었을 때: 확인 위치가 있으면 백그라운드에서 version.txt 를 읽는다 (같은 위치는 한 번만)."""
        location = self.ctx.cfg.update_location if self.ctx.cfg else ""
        if not location:
            self.update_btn.hide()
            self._update_checked_for = ""
            return
        if location == self._update_checked_for:
            return
        self._update_checked_for = location
        self.update_btn.hide()  # 새 위치를 확인하는 동안 옛 알림은 치운다
        self.ctx.runner.run(lambda: update.check(location), self._show_update,
                            on_failed=lambda msg: diagnostics.log.warning("새 버전 확인 실패: %s", msg), quiet=True)

    def _show_update(self, info: Optional[update.UpdateInfo]) -> None:
        self.update_info = info
        if info is None:
            self.update_btn.hide()
            return
        self.update_btn.setText("" if self.sidebar_collapsed else f"새 버전 v{info.version}")
        self.update_btn.setToolTip((info.notes + "\n\n" if info.notes else "") + f"지금 v{__version__}. 눌러서 받는 곳을 엽니다.")
        self.update_btn.show()

    def _open_update(self) -> None:
        if self.update_info is not None:
            open_location(self.update_info.location)

    def _nav_icons(self) -> None:
        for i, (_text, icon) in enumerate(PAGES):
            self.nav.item(i).setIcon(theme.svg_icon(icon, "MUTED", 18, "PRIMARY"))

    def _refresh_projects(self) -> None:
        """설정을 다시 읽었을 때: 프로젝트 목록을 새로 채운다."""
        sw = self.project_switcher
        sw.blockSignals(True)
        sw.clear()
        for name in self.ctx.project_names():
            p = self.ctx.cfg.projects[name]
            kind = p.preset or ("sync" if p.mode == "sync" else "직접 규칙")
            sw.add_project(name, f"{kind}  ·  {p.server_path}" + ("  (Windows)" if p.server_os == "windows" else ""))
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
        self.project_switcher.update()

    def _on_switch(self, index: int) -> None:
        data = self.project_switcher.itemData(index)
        if data in (ADD_PROJECT, IMPORT_LEGACY):
            self._show_project()  # 동작 항목은 선택으로 남기지 않는다
            if data == ADD_PROJECT:
                self.add_project()
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

    def browse_server(self, server: str, path: str) -> None:
        """기록 화면의 [서버 폴더 열기]: 서버 화면으로 가서 그 서버의 그 폴더를 연다."""
        self.go(PAGE_SERVER)
        self.server_page.open_path(server, path)

    def remake(self, record) -> None:
        """기록의 [같은 내용으로 다시 만들기]: 배포 화면 ① 고르기에 같은 MR·파일을 채운다."""
        self.deploy_page.prefill(record)
        self.go(PAGE_DEPLOY)

    def _open_server_settings(self) -> None:
        """배포 완료 화면의 [서버 등록하러 가기]."""
        self.go(PAGE_PROJECTS)
        self.projects_page.tabs.setCurrentIndex(TAB_SERVERS)

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
    diagnostics.setup_logging()
    holder: dict = {}  # 창이 뜨기 전의 오류는 stderr 로, 뜬 뒤에는 창으로
    diagnostics.install_excepthook(lambda: holder["window"].ctx.dialogs if "window" in holder else None)
    app = QApplication.instance() or QApplication(list(argv) if argv is not None else sys.argv)
    app.setStyle("Fusion")
    theme.set_font_scale(str(QSettings().value("ui/font_scale", "normal")))
    theme.apply(app, current_theme_setting())
    # '시스템 설정 따르기'일 때는 Windows 에서 밝게/어둡게를 바꾸면 바로 따라간다
    QGuiApplication.styleHints().colorSchemeChanged.connect(
        lambda *_: current_theme_setting() == "system" and theme.apply(app, "system"))
    window = MainWindow()
    holder["window"] = window
    window.show()
    window.first_run()
    code = app.exec()
    diagnostics.log.info("종료 (%s)", code)
    return code


if __name__ == "__main__":
    sys.exit(main())
