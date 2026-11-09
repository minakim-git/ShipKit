"""프로젝트 설정의 '서버 연결' 탭 (projects_page 에서 떼어 냄).

서버 목록(왼쪽)과 고른 서버의 폼(오른쪽). 바뀌면 changed 를 내고, 지금 값은 .servers (설정 파일 모양의 dict 목록) 로 읽는다.
[연결 시험]·[저장된 비밀번호 지우기]는 배포 완료 화면과 같은 RemoteClient 를 쓴다.
"""
from __future__ import annotations

import copy
from typing import Any, Callable, Dict, List, Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QCheckBox, QComboBox, QHBoxLayout, QLineEdit, QListWidget, QSpinBox, QVBoxLayout, QWidget

from shipkit import notify, remote
from shipkit.config import NOTIFY_LABELS, ConfigError, ServerProfile, parse_servers
from shipkit.gui.context import AppContext
from shipkit.gui.project_widgets import form_layout as _form
from shipkit.gui.remote_panel import RemoteClient
from shipkit.gui.widgets import Card, PathField, button, label, mark_invalid


class ServersTab(QWidget):
    changed = Signal()  # 사용자가 서버를 고치거나 더하거나 지웠을 때

    def __init__(self, ctx: AppContext, project_path: Callable[[], str], server_os: Callable[[], str],
                 project_name: Callable[[], str] = lambda: ""):
        """project_path: 프로젝트의 서버 배포 경로(비워 둔 칸의 안내용), server_os: 지금 설정의 서버 OS (검증용),
        project_name: 알림 시험 메시지에 넣을 프로젝트 이름."""
        super().__init__()
        self.ctx = ctx
        self.client = RemoteClient(ctx)
        self.project_path = project_path
        self.server_os = server_os
        self.project_name = project_name
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
        card = Card("서버 연결")
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
        self.srv_notify = QComboBox()                 # 메신저 알림: 반영·되돌리기가 끝나면 웹훅으로 한 줄
        self.srv_notify.addItem("알리지 않음", "")
        for fmt, text in NOTIFY_LABELS.items():
            self.srv_notify.addItem(f"{text} (JSON 키를 직접 적음)" if fmt == "custom" else text, fmt)
        self.srv_notify.setToolTip("반영·되돌리기가 끝나면 이 서버의 메신저 채널에 결과를 한 줄 보냅니다.\n"
                                   "Slack·Mattermost·Teams 는 {\"text\": …}, 잔디는 {\"body\": …}, Discord 는 {\"content\": …} 로 보냅니다.")
        self.srv_webhook = QLineEdit()
        self.srv_webhook.setPlaceholderText("https://hooks.slack.com/services/…   (메신저에서 만든 수신 웹훅 주소)")
        self.srv_notify_key = QLineEdit()
        self.srv_notify_key.setPlaceholderText("메시지 글을 담을 JSON 키 (예: text)")
        self.srv_notify_key.setMaximumWidth(260)
        for text, widget in (("이름", self.srv_name), ("호스트", self.srv_host), ("포트", self.srv_port),
                             ("접속 계정", self.srv_user), ("인증", self.srv_auth), ("키 파일", self.srv_key),
                             ("올릴 위치", self.srv_upload), ("실행 계정", self.srv_mode), ("계정 이름", self.srv_run_as),
                             ("전환 방법", self.srv_run_with), ("", self.srv_run_password), ("", self.srv_chown),
                             ("서버 배포 경로", self.srv_path), ("메신저 알림", self.srv_notify), ("웹훅 URL", self.srv_webhook),
                             ("JSON 키", self.srv_notify_key)):
            form.addRow(label(text, "fieldLabel", wrap=False), widget)
        right.addLayout(form)
        buttons = QHBoxLayout()
        self.server_test_btn = button("연결 시험")
        self.server_test_btn.clicked.connect(self.test_server)
        self.notify_test_btn = button("알림 시험", "ghost")
        self.notify_test_btn.setToolTip("웹훅 주소로 시험 메시지를 하나 보내 채널에 잘 도착하는지 확인합니다.")
        self.notify_test_btn.clicked.connect(self.test_notify)
        forget = button("저장된 비밀번호 지우기", "ghost")
        forget.clicked.connect(self.forget_server_secrets)
        forget_key = button("서버 지문 잊기", "ghost")
        forget_key.setToolTip("서버를 다시 설치해 지문이 바뀌었을 때. 다음 접속 때 새 지문을 보여 주고 다시 묻습니다.")
        forget_key.clicked.connect(self.forget_host_key)
        buttons.addWidget(self.server_test_btn)
        buttons.addWidget(self.notify_test_btn)
        buttons.addWidget(forget)
        buttons.addWidget(forget_key)
        buttons.addStretch(1)
        right.addLayout(buttons)
        self.srv_error = label("", "fieldError")   # 입력 중 검증: 잘못된 칸과 이유
        self.srv_error.hide()
        right.addWidget(self.srv_error)
        split.addWidget(self.server_form_box, 1)
        card.body.addLayout(split)
        col.addWidget(card)
        col.addStretch(1)

        for edit in (self.srv_name, self.srv_host, self.srv_user, self.srv_upload, self.srv_run_as, self.srv_path,
                     self.srv_key.edit, self.srv_webhook, self.srv_notify_key):
            edit.textChanged.connect(self._server_edited)
        for combo in (self.srv_auth, self.srv_mode, self.srv_run_with, self.srv_notify):
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
        notify_cfg = s.get("notify") if isinstance(s.get("notify"), dict) else {}
        self._select(self.srv_notify, (notify_cfg.get("format") or "slack") if notify_cfg else "")
        self.srv_webhook.setText(str(notify_cfg.get("url", "")))
        self.srv_notify_key.setText(str(notify_cfg.get("key", "")))
        self._loading = was
        self._server_rows()

    def _server_rows(self) -> None:
        """고른 방식에 따라 필요한 칸만 보여 준다."""
        form = self.server_form
        form.setRowVisible(self.srv_key, self.srv_auth.currentData() == "key")
        windows = self.server_os() == "windows"   # Windows OpenSSH 서버: 접속 계정으로만 실행 (sudo/su 없음)
        form.setRowVisible(self.srv_mode, not windows)
        other = self.srv_mode.currentData() == "other" and not windows
        for widget in (self.srv_run_as, self.srv_run_with, self.srv_run_password, self.srv_chown):
            form.setRowVisible(widget, other)
        self.srv_upload.setPlaceholderText("~/deploy 또는 D:/deploy   (이 아래에 날짜 폴더로 올립니다, PowerShell 로 실행)"
                                           if windows else "~/deploy   (이 아래에 날짜 폴더로 올립니다)")
        project_path = self.project_path()
        self.srv_path.setPlaceholderText(f"비우면 프로젝트 값: {project_path}" if project_path else "")
        fmt = self.srv_notify.currentData()
        form.setRowVisible(self.srv_webhook, bool(fmt))
        form.setRowVisible(self.srv_notify_key, fmt == "custom")
        self.notify_test_btn.setVisible(bool(fmt))

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
        fmt = self.srv_notify.currentData()
        if fmt:
            d["notify"] = {"url": self.srv_webhook.text().strip(), "format": fmt}
            if fmt == "custom":
                d["notify"]["key"] = self.srv_notify_key.text().strip()
        return d

    def _server_edited(self, *_args) -> None:
        self._server_rows()
        if self._loading or not (0 <= self._server_row < len(self.servers)):
            return
        self.servers[self._server_row] = self._server_form_dict()
        item = self.server_list.item(self._server_row)
        if item is not None:
            item.setText(self.servers[self._server_row]["name"] or "(이름 없음)")
        self._validate_form()
        self.changed.emit()

    def _validate_form(self) -> None:
        """폼 값을 바로 검사해 잘못된 칸을 빨갛게, 이유를 아래에 보여 준다 (저장 때까지 기다리지 않게)."""
        fields = (("host", self.srv_host), ("user", self.srv_user), ("name", self.srv_name), ("port", self.srv_port),
                  ("upload_dir", self.srv_upload), ("key_file", self.srv_key), ("server_path", self.srv_path),
                  ("run_as", self.srv_run_as), ("notify.key", self.srv_notify_key), ("notify", self.srv_webhook),
                  ("이름", self.srv_name), ("호스트", self.srv_host), ("계정", self.srv_user))
        message = ""
        try:
            parse_servers([self.servers[self._server_row]], "서버", self.server_os())
        except ConfigError as e:
            message = str(e)
        hit = next((w for key, w in fields if message and key in message), None)
        for _key, w in fields:
            mark_invalid(w, message if w is hit else "")
        self.srv_error.setText(message)
        self.srv_error.setVisible(bool(message))

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

    def test_notify(self) -> None:
        """[알림 시험]: 웹훅으로 시험 메시지를 하나 보낸다 (서버 접속은 하지 않는다)."""
        profile = self._current_profile()
        if profile is None:
            return
        if profile.notify is None:
            self.ctx.dialogs.error("알림 시험", "웹훅 URL 을 먼저 적으세요.")
            return
        cfg = profile.notify
        text = notify.test_message(self.project_name() or "프로젝트", profile.name)
        self.notify_test_btn.setEnabled(False)
        self.notify_test_btn.setText("보내는 중…")

        def finish() -> None:
            self.notify_test_btn.setEnabled(True)
            self.notify_test_btn.setText("알림 시험")

        def done(_result) -> None:
            finish()
            self.ctx.dialogs.info("알림 시험", f"{cfg.label} 채널로 보냈습니다. 메신저에서 확인하세요:\n\n{text}")

        def failed(message: str) -> None:
            finish()
            self.ctx.dialogs.error("알림 시험 실패", message)

        self.ctx.runner.run(lambda: notify.send(cfg, text), done, failed, quiet=True)

    def forget_host_key(self) -> None:
        profile = self._current_profile()
        if profile is None:
            return
        if not self.ctx.dialogs.ask("서버 지문 잊기",
                                    f"{profile.host}:{profile.port} 의 기억한 지문을 지울까요?\n"
                                    "다음 접속 때 새 지문을 보여 주고 다시 묻습니다. 서버를 다시 설치한 것이 맞는지 "
                                    "관리자에게 확인한 뒤 지우세요."):
            return
        removed = remote.forget_host_key(profile)
        self.ctx.dialogs.toast("지문을 지웠습니다. 다음 접속 때 다시 확인합니다." if removed else "기억한 지문이 없습니다.",
                               "success" if removed else "info")

    def forget_server_secrets(self) -> None:
        profile = self._current_profile()
        if profile is None:
            return
        removed = self.client.forget(profile)
        self.ctx.dialogs.toast(f"저장된 비밀번호 {removed}개를 지웠습니다." if removed else "저장된 비밀번호가 없습니다.",
                               "success" if removed else "info")
