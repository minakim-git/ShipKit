"""프로젝트 설정의 '서버 연결' 탭 (projects_page 에서 떼어 냄).

서버 목록(왼쪽)과 고른 서버의 폼(오른쪽). 바뀌면 changed 를 내고, 지금 값은 .servers (설정 파일 모양의 dict 목록) 로 읽는다.
[연결 시험]·[저장된 비밀번호 지우기]는 배포 완료 화면과 같은 RemoteClient 를 쓴다.
"""
from __future__ import annotations

import copy
from typing import Any, Callable, Dict, List, Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QCheckBox, QComboBox, QHBoxLayout, QLineEdit, QListWidget, QSpinBox, QVBoxLayout, QWidget

from shipkit.config import ConfigError, ServerProfile, parse_servers
from shipkit.gui.context import AppContext
from shipkit.gui.project_widgets import form_layout as _form
from shipkit.gui.remote_panel import RemoteClient
from shipkit.gui.widgets import Card, PathField, button, label


class ServersTab(QWidget):
    changed = Signal()  # 사용자가 서버를 고치거나 더하거나 지웠을 때

    def __init__(self, ctx: AppContext, project_path: Callable[[], str], server_os: Callable[[], str]):
        """project_path: 프로젝트의 서버 배포 경로(비워 둔 칸의 안내용), server_os: 지금 설정의 서버 OS (검증용)."""
        super().__init__()
        self.ctx = ctx
        self.client = RemoteClient(ctx)
        self.project_path = project_path
        self.server_os = server_os
        self.servers: List[Dict[str, Any]] = []
        self._server_row = -1
        self._loading = False
        self._build()

    @staticmethod
    def _select(combo: QComboBox, value: Any) -> None:
        idx = combo.findData(value or "")
        combo.setCurrentIndex(idx if idx >= 0 else 0)

    def _build(self) -> None:
        col = QVBoxLayout(self)
        col.setContentsMargins(0, 4, 6, 4)
        col.setSpacing(14)
        card = Card("서버 연결", "배포 완료 화면에서 패키지를 바로 올리고 check·apply 까지 실행할 서버입니다 (SSH/SFTP). "
                               "개발·운영처럼 여러 대를 등록할 수 있습니다. 비밀번호는 처음 접속할 때 묻고, "
                               "저장을 고르면 Windows 자격 증명 관리자에 넣어 다음부터 묻지 않습니다.")
        self.note = label("", "muted")
        card.body.addWidget(self.note)
        split = QHBoxLayout()
        split.setSpacing(16)
        left = QVBoxLayout()
        self.server_list = QListWidget()
        self.server_list.setObjectName("serverList")
        self.server_list.setFixedWidth(210)
        self.server_list.currentRowChanged.connect(self._select_server)
        left.addWidget(self.server_list, 1)
        row = QHBoxLayout()
        add = button("+ 서버 추가", "ghost")
        add.clicked.connect(self.add_server)
        self.remove_server_btn = button("지우기", "ghost")
        self.remove_server_btn.clicked.connect(self.remove_server)
        row.addWidget(add)
        row.addWidget(self.remove_server_btn)
        row.addStretch(1)
        left.addLayout(row)
        split.addLayout(left)

        self.server_form_box = QWidget()
        right = QVBoxLayout(self.server_form_box)
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(10)
        form = _form()
        self.server_form = form
        self.srv_name = QLineEdit()
        self.srv_name.setPlaceholderText("예: 개발, 운영")
        self.srv_host = QLineEdit()
        self.srv_host.setPlaceholderText("예: 10.0.0.5 또는 dev.example.com")
        self.srv_port = QSpinBox()
        self.srv_port.setRange(1, 65535)
        self.srv_port.setValue(22)
        self.srv_port.setFixedWidth(110)
        self.srv_port.lineEdit().setTextMargins(6, 0, 0, 0)
        self.srv_user = QLineEdit()
        self.srv_user.setPlaceholderText("SSH 로 접속할 계정")
        self.srv_auth = QComboBox()
        self.srv_auth.addItem("비밀번호", "password")
        self.srv_auth.addItem("SSH 키 파일", "key")
        self.srv_key = PathField(folder=False, placeholder="개인 키 파일 (예: C:/Users/me/.ssh/id_ed25519)")
        self.srv_upload = QLineEdit()
        self.srv_upload.setPlaceholderText("~/deploy   (이 아래에 날짜 폴더로 올립니다)")
        self.srv_mode = QComboBox()
        self.srv_mode.addItem("접속한 계정 그대로", "")
        self.srv_mode.addItem("다른 계정으로 실행", "other")
        self.srv_run_as = QLineEdit()
        self.srv_run_as.setPlaceholderText("서버 스크립트를 실행할 계정")
        self.srv_run_with = QComboBox()
        self.srv_run_with.addItem("sudo -u 계정", "sudo")
        self.srv_run_with.addItem("su - 계정", "su")
        self.srv_run_password = QCheckBox("실행할 때 비밀번호가 필요합니다 (sudo 는 접속 계정, su 는 실행 계정 비밀번호)")
        self.srv_run_password.setToolTip("끄면 비밀번호 없이 실행합니다. 서버가 비밀번호를 물으면 기다리지 않고 바로 실패합니다.")
        self.srv_chown = QCheckBox("올린 패키지 폴더의 소유자를 실행 계정으로 바꿉니다 (chown)")
        self.srv_path = QLineEdit()
        for text, widget in (("이름", self.srv_name), ("호스트", self.srv_host), ("포트", self.srv_port),
                             ("접속 계정", self.srv_user), ("인증", self.srv_auth), ("키 파일", self.srv_key),
                             ("올릴 위치", self.srv_upload), ("실행 계정", self.srv_mode), ("계정 이름", self.srv_run_as),
                             ("전환 방법", self.srv_run_with), ("", self.srv_run_password), ("", self.srv_chown),
                             ("서버 배포 경로", self.srv_path)):
            form.addRow(label(text, "fieldLabel", wrap=False), widget)
        right.addLayout(form)
        right.addWidget(label("다른 계정으로 실행: 접속하는 계정과 서버 스크립트를 실행할 계정이 다를 때 고릅니다. "
                              "이때 올릴 위치는 실행 계정도 들어갈 수 있는 폴더여야 합니다 (예: 실행 계정의 홈 아래).",
                              "muted"))
        buttons = QHBoxLayout()
        self.server_test_btn = button("연결 시험")
        self.server_test_btn.clicked.connect(self.test_server)
        forget = button("저장된 비밀번호 지우기", "ghost")
        forget.clicked.connect(self.forget_server_secrets)
        buttons.addWidget(self.server_test_btn)
        buttons.addWidget(forget)
        buttons.addStretch(1)
        right.addLayout(buttons)
        split.addWidget(self.server_form_box, 1)
        card.body.addLayout(split)
        col.addWidget(card)
        col.addStretch(1)

        for edit in (self.srv_name, self.srv_host, self.srv_user, self.srv_upload, self.srv_run_as, self.srv_path,
                     self.srv_key.edit):
            edit.textChanged.connect(self._server_edited)
        for combo in (self.srv_auth, self.srv_mode, self.srv_run_with):
            combo.currentIndexChanged.connect(self._server_edited)
        self.srv_port.valueChanged.connect(self._server_edited)
        self.srv_run_password.toggled.connect(self._server_edited)
        self.srv_chown.toggled.connect(self._server_edited)

    def load(self, servers: List[Dict[str, Any]], note: str = "") -> None:
        """서버 목록을 채운다. note 는 팀 공유 값을 쓰고 있을 때의 안내 (빈 글이면 숨김)."""
        self.note.setText(note)
        self.note.setVisible(bool(note))
        self.servers = copy.deepcopy(servers)
        self.server_list.blockSignals(True)
        self.server_list.clear()
        for s in self.servers:
            self.server_list.addItem(str(s.get("name") or "(이름 없음)"))
        self.server_list.blockSignals(False)
        self._server_row = -1
        self.server_list.setCurrentRow(0 if self.servers else -1)
        if not self.servers:
            self._select_server(-1)

    def _select_server(self, row: int) -> None:
        self._server_row = row
        has = 0 <= row < len(self.servers)
        self.server_form_box.setEnabled(has)
        self.remove_server_btn.setEnabled(has)
        s = self.servers[row] if has else {}
        was = self._loading
        self._loading = True
        self.srv_name.setText(str(s.get("name", "")))
        self.srv_host.setText(str(s.get("host", "")))
        self.srv_port.setValue(int(s.get("port") or 22))
        self.srv_user.setText(str(s.get("user", "")))
        self._select(self.srv_auth, s.get("auth") or "password")
        self.srv_key.setText(str(s.get("key_file", "")))
        self.srv_upload.setText(str(s.get("upload_dir", "")))
        self._select(self.srv_mode, "other" if s.get("run_as") else "")
        self.srv_run_as.setText(str(s.get("run_as", "")))
        self._select(self.srv_run_with, s.get("run_with") or "sudo")
        self.srv_run_password.setChecked(bool(s.get("run_password")))
        self.srv_chown.setChecked(bool(s.get("chown")))
        self.srv_path.setText(str(s.get("server_path", "")))
        self._loading = was
        self._server_rows()

    def _server_rows(self) -> None:
        """고른 방식에 따라 필요한 칸만 보여 준다."""
        form = self.server_form
        form.setRowVisible(self.srv_key, self.srv_auth.currentData() == "key")
        other = self.srv_mode.currentData() == "other"
        for widget in (self.srv_run_as, self.srv_run_with, self.srv_run_password, self.srv_chown):
            form.setRowVisible(widget, other)
        project_path = self.project_path()
        self.srv_path.setPlaceholderText(f"비우면 프로젝트 값: {project_path}" if project_path else "")

    def _server_form_dict(self) -> Dict[str, Any]:
        """폼의 값 (기본값과 같은 칸은 저장하지 않는다)."""
        d: Dict[str, Any] = {"name": self.srv_name.text().strip(), "host": self.srv_host.text().strip(),
                             "user": self.srv_user.text().strip()}
        if self.srv_port.value() != 22:
            d["port"] = self.srv_port.value()
        if self.srv_auth.currentData() == "key":
            d["auth"] = "key"
            d["key_file"] = self.srv_key.text()
        if self.srv_upload.text().strip():
            d["upload_dir"] = self.srv_upload.text().strip()
        if self.srv_mode.currentData() == "other":
            d["run_as"] = self.srv_run_as.text().strip()
            d["run_with"] = self.srv_run_with.currentData()
            if self.srv_run_password.isChecked():
                d["run_password"] = True
            if self.srv_chown.isChecked():
                d["chown"] = True
        if self.srv_path.text().strip():
            d["server_path"] = self.srv_path.text().strip()
        return d

    def _server_edited(self, *_args) -> None:
        self._server_rows()
        if self._loading or not (0 <= self._server_row < len(self.servers)):
            return
        self.servers[self._server_row] = self._server_form_dict()
        item = self.server_list.item(self._server_row)
        if item is not None:
            item.setText(self.servers[self._server_row]["name"] or "(이름 없음)")
        self.changed.emit()

    def add_server(self) -> None:
        self.servers.append({"name": f"서버 {len(self.servers) + 1}", "host": "", "user": ""})
        self.server_list.addItem(self.servers[-1]["name"])
        self.server_list.setCurrentRow(len(self.servers) - 1)
        self.srv_host.setFocus()
        self.changed.emit()

    def remove_server(self) -> None:
        row = self._server_row
        if not (0 <= row < len(self.servers)):
            return
        if not self.ctx.dialogs.ask("서버 지우기", f"'{self.servers[row].get('name')}' 서버를 목록에서 지울까요?"):
            return
        self.servers.pop(row)
        self.load(self.servers, self.note.text())
        self.changed.emit()

    def _current_profile(self) -> Optional[ServerProfile]:
        """폼의 값으로 만든 서버 (잘못됐으면 알리고 None)."""
        if not (0 <= self._server_row < len(self.servers)):
            return None
        try:
            server_os = self.server_os()
            return parse_servers([self.servers[self._server_row]], "서버", server_os)[0]
        except ConfigError as e:
            self.ctx.dialogs.error("서버 연결", str(e))
            return None

    def test_server(self) -> None:
        profile = self._current_profile()
        if profile is None:
            return
        self.server_test_btn.setEnabled(False)
        self.server_test_btn.setText("시험 중…")

        def finish() -> None:
            self.server_test_btn.setEnabled(True)
            self.server_test_btn.setText("연결 시험")

        def done(text: str) -> None:
            finish()
            self.ctx.dialogs.info("연결 시험", text)

        def failed(text: str) -> None:
            finish()
            self.ctx.dialogs.error("연결 시험 실패", text)

        self.client.test(profile, done, failed, finish)  # 비밀번호 입력을 취소하면 finish

    def forget_server_secrets(self) -> None:
        profile = self._current_profile()
        if profile is None:
            return
        removed = self.client.forget(profile)
        self.ctx.dialogs.info("비밀번호 지우기", f"저장된 비밀번호 {removed}개를 지웠습니다." if removed
                              else "저장된 비밀번호가 없습니다.")
