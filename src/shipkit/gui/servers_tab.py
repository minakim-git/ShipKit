"""프로젝트 설정의 '서버 연결' 탭 (projects_page 에서 떼어 냄).

서버 목록(왼쪽)과 고른 서버의 폼(오른쪽). 바뀌면 changed 를 내고, 지금 값은 .servers (설정 파일 모양의 dict 목록) 로 읽는다.
[연결 시험]·[저장된 비밀번호 지우기]는 배포 완료 화면과 같은 RemoteClient 를 쓴다.
"""
from __future__ import annotations

import copy
import time
from typing import Any, Callable, Dict, List, Optional

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import QCheckBox, QComboBox, QHBoxLayout, QLineEdit, QListWidget, QSpinBox, QVBoxLayout, QWidget

from shipkit import notify, remote
from shipkit.config import HEALTH_WAIT, HEALTH_WAIT_MAX, NOTIFY_DETAILS, NOTIFY_LABELS, ConfigError, ServerProfile, parse_servers
from shipkit.gui.context import AppContext
from shipkit.gui.project_widgets import form_layout as _form
from shipkit.gui.remote_panel import RemoteClient
from shipkit.gui.widgets import PathField, Section, button, label, mark_invalid


class ServersTab(QWidget):
    changed = Signal()  # 사용자가 서버를 고치거나 더하거나 지웠을 때

    def __init__(self, ctx: AppContext, project_path: Callable[[], str], server_os: Callable[[], str],
                 project_name: Callable[[], str] = lambda: ""):
        """project_path: 프로젝트의 서버 배포 경로(비워 둔 칸의 안내용), server_os: 지금 설정의 서버 OS (검증용),
        project_name: 알림 시험 메시지에 넣을 프로젝트 이름."""
        super().__init__()
        self.ctx = ctx
        self.client = RemoteClient(ctx)
        self.last_diagnosis: Optional[remote.Diagnosis] = None   # 마지막 [연결 시험] 결과 (테스트·결과 창)
        self.diagnosis_dialog: Optional[Any] = None
        self.project_path = project_path
        self.server_os = server_os
        self.project_name = project_name
        self.servers: List[Dict[str, Any]] = []
        self._server_row = -1
        self._loading = False
        self._test_run = 0            # 진행 중인 연결 시험 번호 (취소한 뒤 늦게 온 결과는 무시)
        self._test_started = 0.0
        self._test_timer = QTimer(self)
        self._test_timer.setInterval(1000)
        self._test_timer.timeout.connect(self._tick_test)
        self._build()

    @staticmethod
    def _select(combo: QComboBox, value: Any) -> None:
        idx = combo.findData(value or "")
        combo.setCurrentIndex(idx if idx >= 0 else 0)

    def _build(self) -> None:
        col = QVBoxLayout(self)
        col.setContentsMargins(0, 6, 6, 4)
        col.setSpacing(18)
        card = Section()                 # 탭 이름이 곧 제목
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
        self.srv_mode.addItem("접속한 계정으로 실행", "")
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
        self.srv_branch = QLineEdit()                 # 이 서버에 배포할 브랜치: 점검 띠와 서버 반영에서 비교해 경고
        self.srv_branch.setPlaceholderText("비우면 확인하지 않음.  예: master  (이 서버에 배포할 브랜치)")
        self.srv_branch.setToolTip("배포 화면 점검 띠에서 지금 브랜치와 비교하고, 완료 화면에서 다른 브랜치로 만든 패키지를 "
                                   "이 서버에 올리려 하면 한 번 더 묻습니다.")
        self.srv_log = QLineEdit()                    # 서버 로그 파일: 완료 화면 [로그 보기]·서버 화면 [로그 따라가기]가 tail -F
        self.srv_log.setToolTip("반영한 뒤 완료 화면의 [로그 보기]나 서버 화면의 [로그 따라가기]를 누르면 터미널에서 이 파일을 "
                                "tail -F 로 따라갑니다 (마지막 200줄부터, 멈추려면 Ctrl+C).")
        self.srv_health = QLineEdit()                 # 반영 후 확인: 이 PC 에서 URL(HTTP) 또는 포트(TCP)에 접속해 본다
        self.srv_health.setPlaceholderText("비우면 확인하지 않음.  예: http://10.0.0.5:8080/health  또는 포트 번호 8080")
        self.srv_health.setToolTip("반영이 끝나면 이 PC 에서 여기에 접속해 서비스가 살아 있는지 확인합니다. URL 은 HTTP 2xx·3xx 응답,\n"
                                   "포트 번호만 적으면 TCP 연결이 되는지 봅니다. 실패하면 되돌릴지 묻습니다 (명령어는 종료 코드 1).")
        self.srv_health_wait = QSpinBox()
        self.srv_health_wait.setRange(0, HEALTH_WAIT_MAX)
        self.srv_health_wait.setValue(HEALTH_WAIT)
        self.srv_health_wait.setSuffix("초")
        self.srv_health_wait.setToolTip("서비스가 재시작되는 동안 이 시간까지 몇 초마다 다시 봅니다. 0 이면 한 번만.")
        self.srv_health_text = QLineEdit()
        self.srv_health_text.setPlaceholderText("응답에 있어야 할 글 (선택, URL 일 때만. 예: UP)")
        self.srv_health_text.setToolTip("적으면 HTTP 응답 본문에 이 글이 있어야 성공으로 봅니다 (예: Spring Actuator 의 UP).")
        self.srv_health_opts = QWidget()
        opts = QHBoxLayout(self.srv_health_opts)
        opts.setContentsMargins(0, 0, 0, 0)
        opts.setSpacing(8)
        opts.addWidget(label("최대", "muted", wrap=False))
        opts.addWidget(self.srv_health_wait)
        opts.addWidget(label("기다림", "muted", wrap=False))
        opts.addSpacing(8)
        opts.addWidget(self.srv_health_text, 1)
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
        self.srv_notify_detail = QComboBox()          # 반영 알림 아래에 붙일 변경 내역
        for key, text in NOTIFY_DETAILS.items():
            self.srv_notify_detail.addItem(text, key)
        self.srv_notify_detail.setToolTip("반영 알림 아래에 무엇을 올렸는지 붙입니다. MR·PR 제목은 '• !612 제목 (작성자)' 한 줄씩\n"
                                          "(MR 없이 만든 패키지는 패키지 이름), 파일 목록은 '파일: a, b, …' 한 줄 (15개까지).\n"
                                          "되돌리기 알림에는 붙지 않습니다.")
        for text, widget in (("이름", self.srv_name), ("호스트", self.srv_host), ("포트", self.srv_port),
                             ("접속 계정", self.srv_user), ("인증", self.srv_auth), ("키 파일", self.srv_key),
                             ("올릴 위치", self.srv_upload), ("실행 계정", self.srv_mode), ("계정 이름", self.srv_run_as),
                             ("전환 방법", self.srv_run_with), ("", self.srv_run_password), ("", self.srv_chown),
                             ("서버 배포 경로", self.srv_path), ("배포 브랜치", self.srv_branch), ("로그 파일", self.srv_log),
                             ("반영 후 확인", self.srv_health), ("확인 조건", self.srv_health_opts),
                             ("메신저 알림", self.srv_notify), ("웹훅 URL", self.srv_webhook),
                             ("JSON 키", self.srv_notify_key), ("알림 내용", self.srv_notify_detail)):
            form.addRow(label(text, "fieldLabel", wrap=False), widget)
        right.addLayout(form)
        buttons = QHBoxLayout()
        self.server_test_btn = button("연결 시험")
        self.server_test_btn.clicked.connect(self.test_server)
        self.test_cancel_btn = button("취소", "ghost")   # 연결 시험 중에만 보인다
        self.test_cancel_btn.setToolTip("연결 시험을 그만둡니다. 접속을 기다리는 중이면 결과가 와도 무시합니다.")
        self.test_cancel_btn.clicked.connect(self.cancel_test)
        self.test_cancel_btn.hide()
        self.notify_test_btn = button("알림 시험", "ghost")
        self.notify_test_btn.setToolTip("웹훅 주소로 시험 메시지를 하나 보내 채널에 잘 도착하는지 확인합니다.")
        self.notify_test_btn.clicked.connect(self.test_notify)
        self.health_test_btn = button("확인 시험", "ghost")
        self.health_test_btn.setToolTip("반영 후 확인에 적은 URL·포트에 지금 이 PC 에서 한 번 접속해 봅니다.")
        self.health_test_btn.clicked.connect(self.test_health)
        forget = button("저장된 비밀번호 지우기", "ghost")
        forget.clicked.connect(self.forget_server_secrets)
        forget_key = button("서버 지문 잊기", "ghost")
        forget_key.setToolTip("서버를 다시 설치해 지문이 바뀌었을 때. 다음 접속 때 새 지문을 보여 주고 다시 묻습니다.")
        forget_key.clicked.connect(self.forget_host_key)
        buttons.addWidget(self.server_test_btn)
        buttons.addWidget(self.test_cancel_btn)
        buttons.addWidget(self.notify_test_btn)
        buttons.addWidget(self.health_test_btn)
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

        for edit in (self.srv_name, self.srv_host, self.srv_user, self.srv_upload, self.srv_run_as, self.srv_path, self.srv_branch,
                     self.srv_log, self.srv_key.edit, self.srv_webhook, self.srv_notify_key, self.srv_health, self.srv_health_text):
            edit.textChanged.connect(self._server_edited)
        self.srv_health_wait.valueChanged.connect(self._server_edited)
        for combo in (self.srv_auth, self.srv_mode, self.srv_run_with, self.srv_notify, self.srv_notify_detail):
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
        self.srv_branch.setText(str(s.get("branch", "")))
        self.srv_log.setText(str(s.get("log_file", "")))
        health = s.get("health")
        if isinstance(health, dict):
            self.srv_health.setText(str(health.get("url") or health.get("port") or ""))
            try:
                self.srv_health_wait.setValue(int(health.get("wait", HEALTH_WAIT)))
            except (TypeError, ValueError):
                self.srv_health_wait.setValue(HEALTH_WAIT)
            self.srv_health_text.setText(str(health.get("text") or ""))
        else:
            self.srv_health.setText(str(health or ""))
            self.srv_health_wait.setValue(HEALTH_WAIT)
            self.srv_health_text.setText("")
        notify_cfg = s.get("notify") if isinstance(s.get("notify"), dict) else {}
        self._select(self.srv_notify, (notify_cfg.get("format") or "slack") if notify_cfg else "")
        self.srv_webhook.setText(str(notify_cfg.get("url", "")))
        self.srv_notify_key.setText(str(notify_cfg.get("key", "")))
        self._select(self.srv_notify_detail, notify_cfg.get("detail") or "none")
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
        sudo = other and self.srv_run_with.currentData() == "sudo"
        form.setRowVisible(self.srv_chown, other and not sudo)   # sudo 면 실행 계정으로 바로 올리므로 소유자 변경이 필요 없다
        self.srv_upload.setPlaceholderText(
            "~/deploy 또는 D:/deploy   (이 아래에 날짜 폴더로 올립니다, PowerShell 로 실행)" if windows else
            "~/deploy   (실행 계정으로 바로 올리므로 실행 계정이 쓸 수 있는 폴더, ~ 는 실행 계정의 홈)" if sudo else
            "~/deploy   (이 아래에 날짜 폴더로 올립니다)")
        self.srv_log.setPlaceholderText("비우면 없음.  예: D:/tomcat/logs/catalina.out  (반영 뒤 [로그 보기]로 따라갑니다)" if windows else
                                        "비우면 없음.  예: /opt/tomcat/logs/catalina.out  (반영 뒤 [로그 보기]로 따라갑니다)")
        project_path = self.project_path()
        self.srv_path.setPlaceholderText(f"비우면 프로젝트 값: {project_path}" if project_path else "")
        has_health = bool(self.srv_health.text().strip())
        form.setRowVisible(self.srv_health_opts, has_health)
        self.srv_health_text.setVisible(not self.srv_health.text().strip().isdigit())   # 포트만 적으면 '있어야 할 글' 은 없다
        self.health_test_btn.setVisible(has_health)
        fmt = self.srv_notify.currentData()
        form.setRowVisible(self.srv_webhook, bool(fmt))
        form.setRowVisible(self.srv_notify_key, fmt == "custom")
        form.setRowVisible(self.srv_notify_detail, bool(fmt))
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
        if self.srv_branch.text().strip():
            d["branch"] = self.srv_branch.text().strip()
        if self.srv_log.text().strip():
            d["log_file"] = self.srv_log.text().strip()
        health = self.srv_health.text().strip()
        if health:
            d["health"] = {"port": int(health)} if health.isdigit() else {"url": health}
            if self.srv_health_wait.value() != HEALTH_WAIT:
                d["health"]["wait"] = self.srv_health_wait.value()
            if self.srv_health_text.text().strip() and not health.isdigit():
                d["health"]["text"] = self.srv_health_text.text().strip()
        fmt = self.srv_notify.currentData()
        if fmt:
            d["notify"] = {"url": self.srv_webhook.text().strip(), "format": fmt}
            if fmt == "custom":
                d["notify"]["key"] = self.srv_notify_key.text().strip()
            if self.srv_notify_detail.currentData() and self.srv_notify_detail.currentData() != "none":
                d["notify"]["detail"] = self.srv_notify_detail.currentData()
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
                  ("run_as", self.srv_run_as), ("branch", self.srv_branch), ("log_file", self.srv_log),
                  ("notify.key", self.srv_notify_key), ("notify", self.srv_webhook), ("health", self.srv_health),
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
        self._test_run += 1
        run = self._test_run
        self._test_started = time.monotonic()
        self.server_test_btn.setEnabled(False)
        self.server_test_btn.setText("시험 중…  0초")
        self.test_cancel_btn.show()
        self._test_timer.start()

        def finish() -> None:
            if run == self._test_run:
                self._finish_test()

        def done(result) -> None:
            if run != self._test_run:   # 취소한 뒤 늦게 온 결과
                return
            finish()
            if isinstance(result, remote.Diagnosis):
                self.show_diagnosis(profile, result)
            else:
                self.ctx.dialogs.info("연결 시험", result)

        def failed(text: str) -> None:
            if run != self._test_run:
                return
            finish()
            self.ctx.dialogs.error("연결 시험 실패", text)

        self.client.test(profile, done, failed, finish, server_path=self.project_path() or "")  # 비밀번호 입력을 취소하면 finish

    def show_diagnosis(self, profile: ServerProfile, diag: remote.Diagnosis) -> None:
        """진단 결과 창. 설정으로 고칠 수 있는 것은 [추천 설정 적용]으로 폼에 넣는다."""
        from shipkit.gui.diagnosis import DiagnosisDialog
        self.last_diagnosis = diag
        self.diagnosis_dialog = DiagnosisDialog(profile.name, diag, self.window(), on_apply=self.apply_suggestions)
        self.diagnosis_dialog.show()

    def apply_suggestions(self, suggestions: Dict[str, Any]) -> None:
        """진단이 추천한 값(전환 방법·비밀번호 필요·소유자 변경)을 서버 연결 폼에 넣는다. 저장은 사용자가."""
        if "run_with" in suggestions:
            self._select(self.srv_run_with, str(suggestions["run_with"]))
        if "run_password" in suggestions:
            self.srv_run_password.setChecked(bool(suggestions["run_password"]))
        if "chown" in suggestions:
            self.srv_chown.setChecked(bool(suggestions["chown"]))
        self.ctx.dialogs.toast("추천 설정을 폼에 넣었습니다. [저장]을 눌러야 반영됩니다.", "info")

    def _tick_test(self) -> None:
        """연결 시험이 얼마나 걸리고 있는지 단추에 보여 준다 (멈춘 것인지 기다리는 중인지 알 수 있게)."""
        elapsed = int(time.monotonic() - self._test_started)
        hint = "  (접속은 15초, 명령은 출력이 없으면 10분까지 기다립니다)" if elapsed >= 20 else ""
        self.server_test_btn.setText(f"시험 중…  {elapsed}초{hint}")

    def _finish_test(self) -> None:
        self._test_timer.stop()
        self.test_cancel_btn.hide()
        self.server_test_btn.setEnabled(True)
        self.server_test_btn.setText("연결 시험")

    def cancel_test(self) -> None:
        """[취소]: 진행 중인 접속·명령을 끊고 단추를 바로 되돌린다. 아직 접속 중이라 끊을 수 없으면 결과가 와도 무시한다."""
        self._test_run += 1
        self._finish_test()
        self.client.cancel()
        self.ctx.dialogs.toast("연결 시험을 취소했습니다.", "info")

    def test_health(self) -> None:
        """[확인 시험]: 반영 후 확인의 URL·포트에 이 PC 에서 지금 한 번 접속해 본다."""
        profile = self._current_profile()
        if profile is None:
            return
        if profile.health is None:
            self.ctx.dialogs.error("확인 시험", "반영 후 확인에 URL 이나 포트 번호를 먼저 적으세요.")
            return
        cfg = profile.health
        self.health_test_btn.setEnabled(False)
        self.health_test_btn.setText("접속 중…")

        def finish() -> None:
            self.health_test_btn.setEnabled(True)
            self.health_test_btn.setText("확인 시험")

        def done(result) -> None:
            finish()
            ok, detail = result
            if ok:
                self.ctx.dialogs.info("확인 시험", f"{cfg.target} 에 접속됐습니다: {detail}\n\n반영 뒤에도 이렇게 확인합니다.")
            else:
                self.ctx.dialogs.info("확인 시험", f"{cfg.target} 에 지금 접속되지 않습니다: {detail}\n\n"
                                      "서비스가 내려가 있으면 정상입니다. 주소·포트와 이 PC 에서 그 포트로 갈 수 있는지(방화벽)를 확인하세요.")

        def failed(message: str) -> None:
            finish()
            self.ctx.dialogs.error("확인 시험", message)

        self.ctx.runner.run(lambda: remote.health_probe(cfg, profile.host), done, failed, quiet=True)

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
