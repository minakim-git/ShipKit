"""서버 화면: 프로젝트의 서버 연결(SSH/SFTP)로 접속해 서버 파일을 보고, 올리고, 내려받는다.

배포 자동화(배포 화면)와는 별개로 손으로 하는 일 — 로그 받기, 설정 파일 하나 올리기, 폴더 정리 — 을 WinSCP 없이 한다.
서버 연결·지문·비밀번호는 배포 화면과 같은 RemoteClient 를 쓰고, 접속은 한 번 하면 서버를 바꾸거나 끊을 때까지 유지한다.
SFTP 는 한 번에 한 작업만 하므로 작업 중에는 단추를 잠근다. 여기서 한 일은 배포의 백업·되돌리기 대상이 아니다.
"""
from __future__ import annotations

import posixpath
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QFileDialog, QHBoxLayout, QHeaderView, QInputDialog, QLineEdit, QProgressBar,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from shipkit import remote
from shipkit.config import ServerProfile
from shipkit.gui import theme
from shipkit.gui.context import AppContext
from shipkit.gui.remote_panel import RemoteClient
from shipkit.gui.widgets import Card, Chip, button, label, page_header, repolish
from shipkit.history import format_size

ENTRY_ROLE = Qt.ItemDataRole.UserRole


class FileTree(QTreeWidget):
    """서버 파일 목록. 탐색기에서 파일·폴더를 끌어다 놓으면 dropped(경로 목록) 신호."""
    dropped = Signal(list)

    def __init__(self):
        super().__init__()
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            paths = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
            event.acceptProposedAction()
            if paths:
                self.dropped.emit(paths)
            return
        super().dropEvent(event)


class ServerPage(QWidget):
    settings_requested = Signal()   # 서버가 없을 때 [서버 등록하러 가기]

    def __init__(self, ctx: AppContext):
        super().__init__()
        self.ctx = ctx
        self.client = RemoteClient(ctx)
        self.conn: Optional[Any] = None           # 접속한 SSHConnection (끊으면 None)
        self.profile: Optional[ServerProfile] = None
        self.path = ""                             # 지금 보고 있는 서버 폴더 (절대 경로)
        self.entries: List[remote.RemoteEntry] = []
        self.busy = False
        self._pending_path: Optional[str] = None  # 접속이 끝나면 열 폴더 (기록 화면에서 왔을 때)
        self._build()
        ctx.listeners.append(self.refresh_servers)
        ctx.project_listeners.append(self._project_changed)
        self.refresh_servers()

    # ------------------------------------------------------------------ 화면
    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)
        layout.addWidget(page_header("서버", "서버 연결로 접속해 파일을 보고 올리고 내려받습니다. "
                                            "여기서 한 일은 배포의 백업·되돌리기 대상이 아닙니다."))

        self.empty_card = Card()
        self.empty_card.body.addWidget(label("프로젝트 설정의 [서버 연결] 탭에서 서버를 등록하면 여기서 서버 파일을 볼 수 있습니다.", "muted"))
        go = button("서버 등록하러 가기", "ghost")
        go.clicked.connect(self.settings_requested.emit)
        row = QHBoxLayout()
        row.addWidget(go)
        row.addStretch(1)
        self.empty_card.body.addLayout(row)
        layout.addWidget(self.empty_card)

        head = QWidget()
        head_row = QHBoxLayout(head)
        head_row.setContentsMargins(0, 0, 0, 0)
        head_row.setSpacing(8)
        self.server_combo = QComboBox()
        self.server_combo.setMinimumWidth(220)
        self.server_combo.currentIndexChanged.connect(self._server_picked)
        self.prod_chip = Chip("운영 서버", "danger")
        self.prod_chip.hide()
        self.state_chip = Chip("연결 안 함", "neutral")
        self.connect_btn = button("연결", "primary")
        self.connect_btn.clicked.connect(self.toggle_connection)
        head_row.addWidget(self.prod_chip)
        head_row.addWidget(self.state_chip)
        head_row.addWidget(self.server_combo)
        head_row.addWidget(self.connect_btn)
        self.card = Card("서버 파일", right=head)

        tools = QHBoxLayout()
        tools.setSpacing(6)
        self.up_btn = self._icon_button("arrow-up.svg", "위 폴더로")
        self.up_btn.clicked.connect(self.go_up)
        self.refresh_btn = self._icon_button("refresh.svg", "새로고침")
        self.refresh_btn.clicked.connect(self.refresh)
        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText("서버 경로 (예: /app/tomcat/webapps/ROOT)   Enter 로 이동")
        self.path_edit.returnPressed.connect(lambda: self.navigate(self.path_edit.text().strip()))
        self.home_btn = button("홈", "ghost")
        self.home_btn.clicked.connect(lambda: self.navigate("~"))
        self.upload_dir_btn = button("올릴 위치", "ghost")
        self.upload_dir_btn.setToolTip("서버 연결의 '올릴 위치' (패키지를 올리는 곳)")
        self.upload_dir_btn.clicked.connect(lambda: self.profile and self.navigate(self.profile.upload_dir))
        self.target_btn = button("배포 경로", "ghost")
        self.target_btn.setToolTip("서버 배포 경로 (파일이 반영되는 곳)")
        self.target_btn.clicked.connect(lambda: self.navigate(self._target_path()))
        for w in (self.up_btn, self.refresh_btn):
            tools.addWidget(w)
        tools.addWidget(self.path_edit, 1)
        for w in (self.home_btn, self.upload_dir_btn, self.target_btn):
            tools.addWidget(w)
        self.card.body.addLayout(tools)

        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("이름으로 거르기…")
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.textChanged.connect(self._fill_tree)
        self.card.body.addWidget(self.filter_edit)

        self.tree = FileTree()
        self.tree.setObjectName("serverFiles")
        self.tree.setHeaderLabels(["이름", "크기", "수정한 때"])
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(False)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setUniformRowHeights(True)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.itemActivated.connect(self._activated)
        self.tree.itemSelectionChanged.connect(self._update_buttons)
        self.tree.dropped.connect(self.upload_paths)
        self.card.body.addWidget(self.tree, 1)

        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(6)
        self.progress.hide()
        self.card.body.addWidget(self.progress)

        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.upload_btn = button("올리기…")
        self.upload_btn.setToolTip("이 폴더에 파일을 올립니다. 탐색기에서 목록 위로 끌어다 놓아도 됩니다.")
        self.upload_btn.clicked.connect(lambda: self.upload_dialog())
        self.download_btn = button("내려받기")
        self.download_btn.clicked.connect(lambda: self.download())
        self.mkdir_btn = button("새 폴더", "ghost")
        self.mkdir_btn.clicked.connect(lambda: self.make_dir())
        self.rename_btn = button("이름 바꾸기", "ghost")
        self.rename_btn.clicked.connect(lambda: self.rename())
        self.delete_btn = button("지우기", "danger")
        self.delete_btn.clicked.connect(self.delete)
        self.count_label = label("", "muted")
        for w in (self.upload_btn, self.download_btn, self.mkdir_btn, self.rename_btn, self.delete_btn):
            bar.addWidget(w)
        bar.addStretch(1)
        bar.addWidget(self.count_label)
        self.card.body.addLayout(bar)
        layout.addWidget(self.card, 1)
        theme.on_change(self._refresh_icons)
        self._refresh_icons()
        self._set_connected(False)

    @staticmethod
    def _icon_button(icon: str, tooltip: str):
        btn = button("", "ghost")
        btn.setFixedSize(32, 32)
        btn.setToolTip(tooltip)
        btn.setProperty("icon_name", icon)
        return btn

    def _refresh_icons(self) -> None:
        for btn in (self.up_btn, self.refresh_btn):
            btn.setIcon(theme.svg_icon(btn.property("icon_name"), "MUTED", 16))
        self._fill_tree()

    # ------------------------------------------------------------------ 서버 고르기·접속
    def refresh_servers(self) -> None:
        """프로젝트의 서버 목록으로 선택 상자를 채운다 (설정을 다시 읽었을 때, 프로젝트가 바뀌었을 때)."""
        p = self.ctx.project
        servers = p.servers if p else []
        keep = self.server_combo.currentData()
        self.server_combo.blockSignals(True)
        self.server_combo.clear()
        for s in servers:
            self.server_combo.addItem(f"{s.name}  ({s.address})", s.name)
        idx = self.server_combo.findData(keep) if keep else -1
        self.server_combo.setCurrentIndex(idx if idx >= 0 else (0 if servers else -1))
        self.server_combo.blockSignals(False)
        self.empty_card.setVisible(not servers)
        self.card.setVisible(bool(servers))
        if self.profile is not None and self.profile.name not in [s.name for s in servers]:
            self.disconnect()
        self._server_picked()

    def _project_changed(self) -> None:
        self.disconnect()
        self.refresh_servers()

    def current_profile(self) -> Optional[ServerProfile]:
        p = self.ctx.project
        name = self.server_combo.currentData()
        return next((s for s in p.servers if s.name == name), None) if p and name else None

    def _server_picked(self, *_args) -> None:
        profile = self.current_profile()
        self.prod_chip.setVisible(bool(profile and profile.is_production))
        if self.conn is not None and (profile is None or profile.name != self.profile.name):
            self.disconnect()   # 다른 서버를 고르면 지금 접속은 끊는다

    def toggle_connection(self) -> None:
        if self.conn is not None:
            self.disconnect()
        else:
            self.connect()

    def connect(self) -> None:
        """서버 지문 확인 → 비밀번호 준비 → 접속. 끝나면 열려던 폴더(없으면 배포 경로, 그것도 없으면 홈)를 보여 준다."""
        profile = self.current_profile()
        if profile is None or self.busy or self.conn is not None:
            return
        self.profile = profile

        def connected(conn) -> None:
            self.conn = conn
            self._set_connected(True)
            start = self._pending_path or self._target_path() or "~"
            self._pending_path = None
            self.navigate(start, fallback_home=True)

        def failed(message: str) -> None:
            if "로그인에 실패" in message and self.client.forget(profile):
                message += "\n저장해 둔 비밀번호를 지웠습니다. 다시 시도하면 새로 묻습니다."
            self.conn = None
            self._set_connected(False)
            self.ctx.dialogs.error("서버 접속 실패", message)

        def then() -> None:
            secrets = self.client.secrets(profile)
            if secrets is None:
                self._set_connected(False)
                return
            self._run("접속", lambda: self.client.connection_factory(profile, secrets), connected, failed)

        self.state_chip.set("접속 중…", "info")
        self.connect_btn.setEnabled(False)
        self.client.with_host_key(profile, then, failed)

    def disconnect(self) -> None:
        conn, self.conn = self.conn, None
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001  끊는 중의 오류는 중요하지 않다
                pass
        self.profile = None if conn is not None else self.profile
        self.entries = []
        self.path = ""
        self.path_edit.setText("")
        self._fill_tree()
        self._set_connected(False)

    def _set_connected(self, connected: bool) -> None:
        self.connect_btn.setEnabled(True)
        self.connect_btn.setText("연결 끊기" if connected else "연결")
        self.connect_btn.setObjectName("ghost" if connected else "primary")   # 접속 중에는 눈에 덜 띄게
        repolish(self.connect_btn)
        self.state_chip.set("연결됨" if connected else "연결 안 함", "success" if connected else "neutral")
        for w in (self.up_btn, self.refresh_btn, self.path_edit, self.home_btn, self.upload_dir_btn, self.target_btn,
                  self.filter_edit, self.tree, self.upload_btn, self.mkdir_btn):
            w.setEnabled(connected and not self.busy)
        self._update_buttons()

    def _target_path(self) -> str:
        p = self.ctx.project
        if self.profile is None or p is None:
            return ""
        return self.profile.server_path or p.server_path or ""

    # ------------------------------------------------------------------ 백그라운드 작업
    def _run(self, what: str, fn: Callable[[], Any], done: Callable[[Any], None],
             failed: Optional[Callable[[str], None]] = None, progress: bool = False) -> None:
        """SFTP 작업을 백그라운드로 하나씩 돌린다 (동시에 두 개는 안 됨). what: 상태 칩에 보일 말."""
        if self.busy:
            self.ctx.dialogs.toast("앞의 작업이 끝난 뒤에 하세요.", "info")
            return
        self.busy = True
        self.state_chip.set(f"{what} 중…", "info")
        self._lock(True)
        if progress:
            self.progress.setRange(0, 0)
            self.progress.show()

        def finish() -> None:
            self.busy = False
            self.progress.hide()
            self._lock(False)
            if self.conn is not None:
                transport = getattr(self.conn, "client", None) and self.conn.client.get_transport()
                if transport is not None and not transport.is_active():   # 그 사이 끊김
                    self.disconnect()
                    self.ctx.dialogs.toast("서버와 연결이 끊겼습니다. 다시 연결하세요.", "warning")
                    return
                self.state_chip.set("연결됨", "success")
            else:
                self._set_connected(False)

        def on_done(result) -> None:
            finish()
            done(result)

        def on_failed(message: str) -> None:
            finish()
            (failed or (lambda m: self.ctx.dialogs.error(what, m)))(message)

        if progress:
            def stream(emit):
                return fn(lambda a, b: emit((a, b)))
            self.ctx.runner.run_stream(stream, on_done, on_failed, self._progress)
        else:
            self.ctx.runner.run(fn, on_done, on_failed)

    def _progress(self, item) -> None:
        sent, total = item
        self.progress.setRange(0, total)
        self.progress.setValue(sent)

    def _lock(self, busy: bool) -> None:
        connected = self.conn is not None
        for w in (self.up_btn, self.refresh_btn, self.path_edit, self.home_btn, self.upload_dir_btn, self.target_btn,
                  self.tree, self.upload_btn, self.mkdir_btn):
            w.setEnabled(connected and not busy)
        self.connect_btn.setEnabled(not busy)
        self._update_buttons()

    # ------------------------------------------------------------------ 탐색
    def open_path(self, server: str, path: str) -> None:
        """기록 화면의 [서버 폴더 열기]: 그 서버에 접속해(이미 접속돼 있으면 그대로) 그 폴더를 연다."""
        idx = self.server_combo.findData(server)
        if idx < 0:
            self.ctx.dialogs.toast(f"'{server}' 서버가 이 프로젝트의 서버 연결에 없습니다.", "warning")
            return
        self.server_combo.setCurrentIndex(idx)
        if self.conn is not None and self.profile is not None and self.profile.name == server:
            self.navigate(path)
            return
        self._pending_path = path
        self.connect()

    def navigate(self, path: str, fallback_home: bool = False) -> None:
        """그 폴더의 내용을 읽어 보여 준다. fallback_home: 없으면 홈으로."""
        conn = self.conn
        if conn is None or not path:
            return

        def work():
            resolved = conn.resolve(path)
            if not conn.is_dir(resolved):
                raise remote.RemoteError(f"폴더가 아닙니다: {resolved}")
            return resolved, conn.listdir(resolved)

        def done(result) -> None:
            self.path, self.entries = result
            self.path_edit.setText(self.path)
            self.filter_edit.clear()
            self._fill_tree()

        def failed(message: str) -> None:
            if fallback_home and path != "~":
                self.ctx.dialogs.toast(f"{path} 을 열지 못해 홈 폴더를 엽니다.", "warning")
                self.navigate("~")
                return
            self.ctx.dialogs.error("폴더 열기", message)
            self.path_edit.setText(self.path)

        self._run("읽는", work, done, failed)

    def refresh(self) -> None:
        if self.path:
            self.navigate(self.path)

    def go_up(self) -> None:
        if self.path and self.path not in ("/", ""):
            parent = posixpath.dirname(self.path.rstrip("/")) or "/"
            if parent != self.path:
                self.navigate(parent)

    def _activated(self, item: QTreeWidgetItem, _col: int) -> None:
        entry = item.data(0, ENTRY_ROLE)
        if entry is None:
            return
        if entry.is_dir:
            self.navigate(entry.path)
        else:
            self.download([entry])

    def _fill_tree(self, *_args) -> None:
        text = self.filter_edit.text().strip().lower()
        self.tree.clear()
        shown = 0
        folder_icon, file_icon = theme.svg_icon("folder.svg", "PRIMARY", 16), theme.svg_icon("file.svg", "MUTED", 16)
        for e in self.entries:
            if text and text not in e.name.lower():
                continue
            when = datetime.fromtimestamp(e.mtime).strftime("%Y-%m-%d %H:%M") if e.mtime else ""
            item = QTreeWidgetItem([e.name + ("  →" if e.link else ""), "" if e.is_dir else format_size(e.size), when])
            item.setIcon(0, folder_icon if e.is_dir else file_icon)
            item.setTextAlignment(1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            item.setData(0, ENTRY_ROLE, e)
            item.setToolTip(0, e.path)
            self.tree.addTopLevelItem(item)
            shown += 1
        dirs = sum(1 for e in self.entries if e.is_dir)
        files = len(self.entries) - dirs
        total = f"폴더 {dirs}개 · 파일 {files}개" if self.entries else ""
        self.count_label.setText(f"{shown}개 보임 · {total}" if text and self.entries else total)
        self._update_buttons()

    def selected(self) -> List[remote.RemoteEntry]:
        return [i.data(0, ENTRY_ROLE) for i in self.tree.selectedItems() if i.data(0, ENTRY_ROLE) is not None]

    def _update_buttons(self) -> None:
        ready = self.conn is not None and not self.busy
        picked = self.selected() if ready else []
        self.download_btn.setEnabled(bool(picked))
        self.rename_btn.setEnabled(len(picked) == 1)
        self.delete_btn.setEnabled(bool(picked))

    # ------------------------------------------------------------------ 올리기·내려받기·폴더·이름·지우기
    def upload_dialog(self, paths: Optional[List[str]] = None) -> None:
        if self.conn is None:
            return
        if paths is None:
            paths, _f = QFileDialog.getOpenFileNames(self, f"{self.path} 에 올릴 파일", "", "모든 파일 (*)")
        if paths:
            self.upload_paths(paths)

    def upload_paths(self, paths: List[str]) -> None:
        """파일·폴더들을 지금 폴더에 같은 이름으로 올린다 (끌어다 놓기, [올리기…])."""
        conn, folder = self.conn, self.path
        if conn is None or not folder or not paths:
            return
        local = [Path(p) for p in paths]
        existing = {e.name for e in self.entries}
        clash = [p.name for p in local if p.name in existing]
        if clash and not self.ctx.dialogs.ask(
                "덮어쓰기", f"{folder} 에 같은 이름이 이미 있습니다:\n" + "\n".join(f"- {n}" for n in clash[:10])
                + ("\n…" if len(clash) > 10 else "") + "\n\n덮어쓸까요? (폴더는 안의 같은 이름 파일만 바뀝니다)"):
            return

        def done(count: int) -> None:
            self.ctx.dialogs.toast(f"파일 {count}개를 {folder} 에 올렸습니다.")
            self.navigate(folder)

        self._run("올리는", lambda progress: conn.upload_paths(local, folder, progress), done, progress=True)

    def download(self, entries: Optional[List[remote.RemoteEntry]] = None, target_dir: Optional[str] = None) -> None:
        """고른 파일·폴더를 PC 폴더에 같은 이름으로 내려받는다."""
        conn = self.conn
        entries = entries if entries is not None else self.selected()
        if conn is None or not entries:
            return
        if target_dir is None:
            target_dir = QFileDialog.getExistingDirectory(self, "내려받을 폴더")
        if not target_dir:
            return
        dest = Path(target_dir)

        def work(progress):
            count = 0
            for i, e in enumerate(entries):
                count += conn.download(e, dest, lambda a, b: progress(i * 1000 + a * 1000 // max(b, 1), len(entries) * 1000))
            return count

        def done(count: int) -> None:
            self.ctx.dialogs.toast(f"파일 {count}개를 {dest} 에 내려받았습니다.")

        self._run("내려받는", work, done, progress=True)

    def make_dir(self, name: Optional[str] = None) -> None:
        conn, folder = self.conn, self.path
        if conn is None or not folder:
            return
        if name is None:
            name, ok = QInputDialog.getText(self, "새 폴더", f"{folder} 안에 만들 폴더 이름")
            if not ok:
                return
        name = (name or "").strip().strip("/")
        if not name or "/" in name or name in (".", ".."):
            self.ctx.dialogs.error("새 폴더", "폴더 이름에는 / 를 쓸 수 없습니다.")
            return
        self._run("만드는", lambda: conn.mkdir(posixpath.join(folder, name)), lambda _r: self.navigate(folder))

    def rename(self, new_name: Optional[str] = None) -> None:
        conn, folder = self.conn, self.path
        picked = self.selected()
        if conn is None or len(picked) != 1:
            return
        entry = picked[0]
        if new_name is None:
            new_name, ok = QInputDialog.getText(self, "이름 바꾸기", f"'{entry.name}' 의 새 이름", text=entry.name)
            if not ok:
                return
        new_name = (new_name or "").strip()
        if not new_name or "/" in new_name or new_name in (".", ".."):
            self.ctx.dialogs.error("이름 바꾸기", "이름이 비었거나 / 가 들어 있습니다.")
            return
        if new_name == entry.name:
            return
        if any(e.name == new_name for e in self.entries):
            self.ctx.dialogs.error("이름 바꾸기", f"같은 이름이 이미 있습니다: {new_name}")
            return
        self._run("바꾸는", lambda: conn.rename(entry.path, posixpath.join(folder, new_name)),
                  lambda _r: self.navigate(folder))

    def delete(self) -> None:
        conn, folder = self.conn, self.path
        picked = self.selected()
        if conn is None or not picked:
            return
        names = "\n".join(f"- {e.name}{'/' if e.is_dir else ''}" for e in picked[:10]) + ("\n…" if len(picked) > 10 else "")
        warn = "⚠ 운영 서버입니다.\n\n" if self.profile is not None and self.profile.is_production else ""
        if not self.ctx.dialogs.ask("서버에서 지우기", f"{warn}{folder} 에서 {len(picked)}개를 지웁니다. 폴더는 안의 것까지 지우며 "
                                                 f"되돌릴 수 없습니다.\n\n{names}\n\n지울까요?"):
            return

        def work():
            return sum(conn.remove(e) for e in picked)

        def done(count: int) -> None:
            self.ctx.dialogs.toast(f"{count}개를 지웠습니다.")
            self.navigate(folder)

        self._run("지우는", work, done)
