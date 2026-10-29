"""연결 화면: GitLab·GitHub 주소와 토큰(Windows 자격 증명 관리자). MR·PR 로 패키지를 만들 때 쓴다."""
from __future__ import annotations

import copy
from typing import Any, Dict, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QCheckBox, QFormLayout, QHBoxLayout, QLineEdit, QScrollArea, QSpinBox, QVBoxLayout, QWidget

from shipkit import config_store
from shipkit.config import GITHUB_API_URL, ConfigError, GitHubConfig, GitLabConfig
from shipkit.gui.context import AppContext
from shipkit.gui.widgets import Card, Chip, PathField, button, label, page_header
from shipkit.secrets import SecretError, delete_token, get_token, set_token, token_source
from shipkit.sources.github import GitHubClient
from shipkit.sources.gitlab import GitLabClient


def _form() -> QFormLayout:
    form = QFormLayout()
    form.setHorizontalSpacing(16)
    form.setVerticalSpacing(10)
    form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    return form


class ServiceBox(Card):
    """GitLab 또는 GitHub 연결 설정 카드."""

    def __init__(self, page: "ConnectionsPage", key: str, title: str, subtitle: str, url_hint: str,
                 default_url: str = ""):
        self.enabled = QCheckBox("사용")
        self.enabled.setObjectName("switch")
        self.enabled.setCursor(Qt.CursorShape.PointingHandCursor)
        super().__init__(title, subtitle, right=self.enabled)
        self.page, self.key, self.default_url = page, key, default_url
        self.url = QLineEdit()
        self.url.setPlaceholderText(url_hint)
        self.verify = QCheckBox("SSL 인증서 검증 (권장)")
        self.verify.setChecked(True)
        self.ca = PathField(folder=False, placeholder="사내 인증서(CA) 파일 - 필요할 때만",
                            file_filter="인증서 (*.pem *.crt *.cer);;모든 파일 (*)")
        self.timeout = QSpinBox()
        self.timeout.setRange(3, 300)
        self.timeout.setValue(15)
        self.timeout.setSuffix(" 초")
        self.timeout.setFixedWidth(110)
        self.timeout.lineEdit().setTextMargins(6, 0, 0, 0)

        self.token_chip = Chip("", "neutral")
        self.token_label = self.token_chip
        save_token = button("토큰 저장")
        save_token.clicked.connect(self.save_token)
        remove_token = button("토큰 삭제", "danger")
        remove_token.clicked.connect(self.remove_token)
        test = button("연결 테스트", "ghost")
        test.clicked.connect(self.test_connection)
        token_row = QHBoxLayout()
        token_row.setSpacing(8)
        token_row.addWidget(self.token_chip)
        token_row.addStretch(1)
        for b in (test, save_token, remove_token):
            token_row.addWidget(b)

        form = _form()
        form.addRow(label("주소", "fieldLabel", wrap=False), self.url)
        form.addRow(label("", wrap=False), self.verify)
        form.addRow(label("CA 파일", "fieldLabel", wrap=False), self.ca)
        form.addRow(label("시간 제한", "fieldLabel", wrap=False), self.timeout)
        form.addRow(label("토큰", "fieldLabel", wrap=False), token_row)
        self.body.addLayout(form)
        self.enabled.toggled.connect(self._toggle)

    def _toggle(self, on: bool) -> None:
        for w in (self.url, self.verify, self.ca, self.timeout):
            w.setEnabled(on)
        self.update_token_status()

    def load(self, raw: Optional[Dict[str, Any]], present: bool) -> None:
        raw = raw or {}
        self.enabled.setChecked(present)
        self.url.setText(str(raw.get("url") or (self.default_url if present else "")))
        self.verify.setChecked(bool(raw.get("verify_ssl", True)))
        self.ca.setText(str(raw.get("ca_bundle") or ""))
        self.timeout.setValue(int(raw.get("timeout", 15)))
        self._toggle(present)

    def dump(self, raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not self.enabled.isChecked():
            return None
        out = dict(raw or {})
        out["url"] = self.url.text().strip() or self.default_url
        out["verify_ssl"] = self.verify.isChecked()
        out["ca_bundle"] = self.ca.text() or None
        out["timeout"] = self.timeout.value()
        return out

    def service_config(self):
        url = (self.url.text().strip() or self.default_url).rstrip("/")
        if not url:
            raise ConfigError(f"{self.key} 주소를 입력하세요.")
        cls = GitLabConfig if self.key == "gitlab" else GitHubConfig
        existing = getattr(self.page.ctx.cfg, self.key, None) if self.page.ctx.cfg else None
        token_env = existing.token_env if existing else cls.__dataclass_fields__["token_env"].default
        return cls(url=url, token_env=token_env, verify_ssl=self.verify.isChecked(),
                   ca_bundle=self.ca.text() or None, timeout=float(self.timeout.value()))

    def update_token_status(self) -> None:
        if not self.enabled.isChecked():
            self.token_chip.set("사용 안 함", "neutral")
            return
        try:
            src = token_source(self.service_config())
        except Exception:
            src = None
        if src:
            self.token_chip.set(f"토큰 저장됨 · {src}", "success")
        elif self.key == "github":
            self.token_chip.set("토큰 없음 · 공개 저장소만 조회", "warning")
        else:
            self.token_chip.set("토큰 없음", "danger")

    def save_token(self) -> None:
        dialogs = self.page.ctx.dialogs
        try:
            cfg = self.service_config()
        except ConfigError as e:
            dialogs.error("토큰 저장", str(e))
            return
        token = dialogs.password("토큰 저장", f"{cfg.url} 의 {cfg.label} 토큰을 붙여넣으세요 (화면에 표시되지 않음)")
        if not token:
            return
        try:
            set_token(cfg, token)
        except SecretError as e:
            dialogs.error("토큰 저장 실패", str(e))
            return
        self.update_token_status()
        self.page.ctx.connections_changed()
        dialogs.info("토큰 저장", f"{cfg.label} 토큰을 Windows 자격 증명 관리자에 저장했습니다.")

    def remove_token(self) -> None:
        dialogs = self.page.ctx.dialogs
        try:
            cfg = self.service_config()
            if dialogs.ask("토큰 삭제", f"{cfg.url} 의 {cfg.label} 토큰을 지울까요?"):
                removed = delete_token(cfg)
                dialogs.info("토큰 삭제", "삭제했습니다." if removed else "저장된 토큰이 없습니다.")
        except (ConfigError, SecretError) as e:
            dialogs.error("토큰 삭제 실패", str(e))
        self.update_token_status()
        self.page.ctx.connections_changed()

    def test_connection(self) -> None:
        ctx = self.page.ctx
        try:
            cfg = self.service_config()
        except ConfigError as e:
            ctx.dialogs.error("연결 테스트", str(e))
            return

        def run():
            if self.key == "gitlab":
                user = GitLabClient(cfg, get_token(cfg)).current_user()
                return f"GitLab 연결 성공: {user.get('username')} ({user.get('name')})"
            token = get_token(cfg, required=False)
            client = GitHubClient(cfg, token)
            if token:
                return f"GitHub 연결 성공: {client.current_user().get('login')}"
            client.get_repo("octocat/Hello-World")
            return "GitHub 연결 성공 (토큰 없이 공개 저장소만 조회 가능)"

        ctx.runner.run(run, lambda msg: ctx.dialogs.info("연결 테스트", msg),
                       lambda msg: ctx.dialogs.error("연결 테스트 실패", msg))


class ConnectionsPage(QWidget):
    def __init__(self, ctx: AppContext):
        super().__init__()
        self.ctx = ctx
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(14)
        self.save_btn = button("연결 저장", "primary")
        self.save_btn.setMinimumWidth(120)
        self.save_btn.clicked.connect(self.save)
        outer.addWidget(page_header(
            "연결", "MR·PR 목록을 불러올 GitLab·GitHub 를 연결합니다. 토큰은 설정 파일이 아니라 "
                   "Windows 자격 증명 관리자에 저장합니다.", self.save_btn))

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 6, 0)
        layout.setSpacing(14)
        self.gitlab = ServiceBox(self, "gitlab", "GitLab", "MR 로 패키지를 만들 때 씁니다. 토큰은 read_api 권한이면 됩니다.",
                                 "예: https://gitlab.example.com")
        self.github = ServiceBox(self, "github", "GitHub", "PR 로 패키지를 만들 때 씁니다.",
                                 f"비우면 {GITHUB_API_URL}  (Enterprise 는 https://<호스트>/api/v3)", GITHUB_API_URL)
        layout.addWidget(self.gitlab)
        layout.addWidget(self.github)
        layout.addWidget(label("주소를 바꾸면 [연결 저장]을 누른 뒤 토큰을 저장하세요. 토큰은 주소별로 저장됩니다.", "muted"))
        layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(content)
        scroll.viewport().setAutoFillBackground(False)
        content.setAutoFillBackground(False)
        outer.addWidget(scroll, 1)
        ctx.listeners.append(self.refresh)

    def refresh(self) -> None:
        try:
            data = config_store.load_raw(self.ctx.config_path)
        except ConfigError:
            data = {}
        self.gitlab.load(data.get("gitlab"), "gitlab" in data)
        self.github.load(data.get("github"), "github" in data)

    def save(self) -> None:
        """연결(gitlab·github)만 저장한다. 다른 값은 그대로 둔다."""
        data = config_store.load_raw(self.ctx.config_path)
        before = copy.deepcopy(data)
        for box in (self.gitlab, self.github):
            dumped = box.dump(data.get(box.key) or {})
            if dumped is None:
                data.pop(box.key, None)
            else:
                data[box.key] = dumped
        try:
            config_store.save_raw(self.ctx.config_path, data)
        except ConfigError as e:
            self.ctx.dialogs.error("저장하지 못했습니다", str(e))
            return
        if data != before or not before:
            self.ctx.reload()
        self.gitlab.update_token_status()
        self.github.update_token_status()
        self.ctx.connections_changed()
        self.ctx.dialogs.info("연결 저장", "연결 설정을 저장했습니다.")
