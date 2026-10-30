"""서버에 바로 반영 (SSH/SFTP): 배포 완료 화면의 [올리고 확인] → [백업 후 반영] → [되돌리기], 프로젝트 설정의 [연결 시험].

비밀번호는 필요할 때 묻고, [이 PC 에 저장]을 고르면 Windows 자격 증명 관리자에 넣어 다음부터 묻지 않는다.
로그인에 실패하면 저장해 둔 비밀번호를 지워서 다음에 다시 묻게 한다.
처음 접속하는 서버는 지문을 보여 주고 확인을 받는다. 기억한 지문과 다르면 접속하지 않는다.
"""
from __future__ import annotations

import shlex
import threading
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QPlainTextEdit, QProgressBar, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from shipkit import history, remote
from shipkit.config import ProjectConfig, ServerProfile
from shipkit.gui import theme
from shipkit.gui.context import AppContext
from shipkit.gui.widgets import Card, Chip, button, label
from shipkit.secrets import SecretError, delete_secret, get_secret, server_secret_key, set_secret

HOOK_TEXT = {"pre_apply": "반영 전", "post_apply": "반영 후", "post_rollback": "되돌린 후"}


_secret_slots = remote.secret_slots   # 어떤 비밀번호가 필요한지는 명령어(shipkit deploy)와 같은 규칙


class RemoteClient:
    """비밀번호 준비·서버 지문 확인·백그라운드 실행을 묶은 도우미 (화면 스레드에서 쓴다)."""

    def __init__(self, ctx: AppContext):
        self.ctx = ctx
        self.connection_factory: Callable[[ServerProfile, remote.Secrets], Any] = remote.SSHConnection
        self.host_key_checker: Callable[[ServerProfile], remote.HostKeyStatus] = remote.check_host_key
        self._trusted: Set[Tuple[str, int]] = set()
        self._active: Optional[Any] = None   # 지금 작업 중인 연결 (취소용)
        self._lock = threading.Lock()

    def cancel(self) -> bool:
        """진행 중인 올리기·실행을 끊는다. 반환: 끊을 작업이 있었는지."""
        with self._lock:
            conn = self._active
        if conn is None:
            return False
        conn.cancel()
        return True

    def secrets(self, profile: ServerProfile) -> Optional[remote.Secrets]:
        """필요한 비밀번호를 모은다 (저장돼 있으면 그대로, 없으면 묻는다). 취소하면 None."""
        found: Dict[str, Optional[str]] = {"login": None, "run": None}
        try:
            slots = _secret_slots(profile)
        except remote.RemoteError as e:
            self.ctx.dialogs.error("서버 연결", str(e))
            return None
        for kind, key, text in slots:
            try:
                value = get_secret(key)
            except SecretError:
                value = None
            if value is None:
                answer = self.ctx.dialogs.ask_secret("비밀번호 입력", f"{profile.name} 서버: {text}")
                if answer is None:
                    return None
                value, save = answer
                if save:
                    try:
                        set_secret(key, value)
                    except SecretError as e:
                        self.ctx.dialogs.error("비밀번호 저장 실패", f"{e}\n이번에만 쓰고 저장하지 않습니다.")
            found[kind] = value
        return remote.Secrets(login=found["login"], run=found["run"])

    def forget(self, profile: ServerProfile) -> int:
        """이 서버에 저장한 비밀번호를 모두 지운다. 반환: 지운 개수."""
        p = profile
        keys = [server_secret_key(k, p.user, p.host, p.port) for k in ("login", "passphrase", "sudo")]
        if p.run_as:
            keys.append(server_secret_key(f"su:{p.run_as}", p.user, p.host, p.port))
        return sum(delete_secret(k) for k in keys)

    def with_host_key(self, profile: ServerProfile, then: Callable[[], None],
                      failed: Callable[[str], None]) -> None:
        """서버 지문을 확인한 뒤 then() (처음이면 사용자에게 묻고 기억한다)."""
        if (profile.host, profile.port) in self._trusted:
            then()
            return

        def checked(status: remote.HostKeyStatus) -> None:
            if status.state == "changed":
                failed(f"{profile.host} 의 서버 지문이 예전에 기억한 것과 다릅니다. 다른 서버로 연결될 위험이 있어 "
                       f"접속하지 않았습니다.\n지금 지문: {status.fingerprint}\n서버를 다시 설치했다면 서버 관리자에게 확인한 뒤 "
                       f"{remote.known_hosts_path()} 에서 그 줄을 지우세요.")
                return
            if status.state == "new":
                if not self.ctx.dialogs.ask(
                        "새 서버 확인",
                        f"{profile.name} ({profile.host}:{profile.port}) 에 처음 접속합니다.\n\n"
                        f"서버 지문 ({status.key_type})\n{status.fingerprint}\n\n"
                        "이 서버가 맞으면 [예]를 누르세요. 기억해 두고 다음부터는 묻지 않습니다."):
                    failed("서버 지문을 확인하지 않아 접속하지 않았습니다.")
                    return
                remote.remember_host_key(profile, status)
            self._trusted.add((profile.host, profile.port))
            then()

        self.ctx.runner.run(lambda: self.host_key_checker(profile), checked, failed)

    def stream(self, profile: ServerProfile, secrets: remote.Secrets,
               step: Callable[[Any, Callable[[str], None], Callable[[int, int], None]], Any],
               on_output: Callable[[str], None], on_progress: Callable[[int, int], None],
               on_done: Callable[[Any], None], on_failed: Callable[[str], None],
               on_cancelled: Optional[Callable[[str], None]] = None) -> None:
        """접속해서 step(conn, output, progress) 을 백그라운드로 실행한다.
        로그인 실패는 ('auth', 글), [취소]로 끊은 것은 ('cancelled', 글) 로 돌려받아 각각 처리한다."""

        def work(emit):
            try:
                conn = self.connection_factory(profile, secrets)
            except remote.RemoteAuthError as e:
                return ("auth", str(e))
            with self._lock:
                self._active = conn
            try:
                return step(conn, lambda text: emit(("out", text)), lambda a, b: emit(("progress", a, b)))
            except remote.RemoteCancelled as e:
                return ("cancelled", str(e))
            finally:
                with self._lock:
                    self._active = None
                conn.close()

        def progress(item) -> None:
            if item[0] == "out":
                on_output(item[1])
            else:
                on_progress(item[1], item[2])

        def done(result) -> None:
            if isinstance(result, tuple) and result and result[0] == "auth":
                removed = self.forget(profile)
                on_failed(result[1] + ("\n저장해 둔 비밀번호를 지웠습니다. 다시 시도하면 새로 묻습니다." if removed else ""))
                return
            if isinstance(result, tuple) and result and result[0] == "cancelled":
                on_output(f"\n[취소] {result[1]}\n")
                (on_cancelled or on_failed)(result[1])
                return
            on_done(result)

        self.ctx.runner.run_stream(work, done, on_failed, progress)

    def start(self, profile: ServerProfile, make_step, on_output: Callable[[str], None],
              on_progress: Callable[[int, int], None], on_done: Callable[[Any], None],
              on_failed: Callable[[str], None], on_cancel: Callable[[], None]) -> None:
        """서버 지문 확인 → 비밀번호 준비(필요하면 묻기) → make_step(secrets) 로 만든 단계를 실행.
        지문을 먼저 확인해서, 엉뚱한 서버에 비밀번호를 넣는 일이 없게 한다.
        on_cancel: 비밀번호 입력을 취소했거나 [취소]로 작업을 끊었을 때."""

        def connect() -> None:
            secrets = self.secrets(profile)
            if secrets is None:
                on_cancel()
                return
            self.stream(profile, secrets, make_step(secrets), on_output, on_progress, on_done, on_failed,
                        on_cancelled=lambda _msg: on_cancel())

        self.with_host_key(profile, connect, on_failed)

    def test(self, profile: ServerProfile, done: Callable[[str], None], failed: Callable[[str], None],
             cancelled: Callable[[], None]) -> None:
        """[연결 시험]: 로그인, 실행 계정 전환, 올릴 위치 확인까지."""

        def make_step(secrets):
            return lambda conn, output, progress: step(conn, secrets)

        def step(conn, secrets):
            lines: List[str] = []
            home = conn.home()
            upload = remote.expand_dir(profile.upload_dir, home)
            opts = remote.run_password(profile, secrets)
            code = conn.run(remote.as_runner(profile, "id -un", opts["password"] is not None), lines.append, **opts)
            who = "".join(lines).strip().splitlines()[-1:] or [""]
            if code or who[0].strip() != profile.runner:
                return False, f"{profile.runner} 계정으로 실행하지 못했습니다 (종료 코드 {code}): {''.join(lines).strip()}"
            probe = f"mkdir -p {shlex.quote(upload)} && test -w {shlex.quote(upload)}"
            if conn.run(probe, lines.append):
                return False, f"올릴 위치 {upload} 에 쓸 수 없습니다 ({profile.user} 계정)."
            return True, f"접속 성공: {profile.address}\n실행 계정: {profile.runner}\n올릴 위치: {upload}"

        def finished(result) -> None:
            ok, text = result
            (done if ok else failed)(text)

        self.start(profile, make_step, lambda t: None, lambda a, b: None, finished, failed, cancelled)


class RemotePanel(Card):
    """배포 완료 화면의 '서버에 바로 반영' 카드."""
    settings_requested = Signal()  # 서버가 없을 때 [서버 등록하러 가기]

    def __init__(self, ctx: AppContext):
        self.server_combo = QComboBox()
        self.server_combo.setMinimumWidth(220)
        super().__init__("서버에 바로 반영", "패키지를 서버에 올리고 check → backup·apply 까지 여기서 실행합니다.",
                         right=self.server_combo, collapsible=True)
        self.ctx = ctx
        self.client = RemoteClient(ctx)
        self.result = None                 # 방금 만든 패키지 (PackageResult)
        self.remote_dir: Optional[str] = None
        self.stage = "ready"               # ready / checked / applied

        self.empty = label("프로젝트 설정의 [서버 연결] 탭에서 서버를 등록하면 여기서 바로 올리고 반영할 수 있습니다.", "muted")
        go = button("서버 등록하러 가기", "ghost")
        go.clicked.connect(self.settings_requested.emit)
        self.empty_row = QWidget()
        row = QHBoxLayout(self.empty_row)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.empty, 1)
        row.addWidget(go)
        self.body.addWidget(self.empty_row)

        self.main = QWidget()
        col = QVBoxLayout(self.main)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(8)
        self.target_label = label("", "muted")
        col.addWidget(self.target_label)
        buttons = QHBoxLayout()
        # 글자가 길면 세 버튼이 좁은 창(1000px)에 들어가지 않아서, 스크립트 명령 이름은 말풍선으로 뺐다
        self.check_btn = button("올리고 확인", "primary")
        self.check_btn.setToolTip("패키지를 서버에 올린 뒤 shipkit.sh check 를 실행합니다 (서버는 바뀌지 않음)")
        self.check_btn.clicked.connect(self.upload_and_check)
        self.apply_btn = button("백업 후 반영")
        self.apply_btn.setToolTip("shipkit.sh backup → apply: 바뀌거나 지워질 파일을 먼저 백업하고 반영합니다")
        self.apply_btn.clicked.connect(self.apply)
        self.rollback_btn = button("되돌리기", "danger")
        self.rollback_btn.setToolTip("shipkit.sh rollback: 백업 기준으로 반영 전 상태로 복구합니다")
        self.rollback_btn.clicked.connect(self.rollback)
        self.state_chip = Chip("", "neutral")
        self.cancel_btn = button("취소", "ghost")   # 진행 중일 때만 보인다
        self.cancel_btn.setToolTip("올리기·실행을 중단합니다. 서버에서 이미 시작된 명령은 그대로 끝까지 돌 수 있습니다.")
        self.cancel_btn.clicked.connect(self.cancel)
        self.cancel_btn.hide()
        for b in (self.check_btn, self.apply_btn, self.rollback_btn):
            buttons.addWidget(b)
        buttons.addStretch(1)
        col.addLayout(buttons)
        status = QHBoxLayout()  # 상태 배지와 [취소]는 둘째 줄 (버튼과 한 줄에 두면 좁은 창에서 오른쪽이 잘린다)
        status.setSpacing(6)
        status.addWidget(self.state_chip, 0, Qt.AlignmentFlag.AlignVCenter)
        status.addWidget(self.cancel_btn)
        self.check_chips = QHBoxLayout()  # check 결과 개수 배지(신규 N, 변경 N …)도 같은 줄에
        self.check_chips.setSpacing(6)
        status.addLayout(self.check_chips)
        status.addStretch(1)
        col.addLayout(status)
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.hide()
        col.addWidget(self.progress)
        # check 결과 표: 로그를 읽어 상태별(신규·변경·동일·삭제예정·이미없음·정리예정)로 묶어 보여 준다
        self.check_result: Optional[remote.CheckResult] = None
        self.check_box = QWidget()
        check_col = QVBoxLayout(self.check_box)
        check_col.setContentsMargins(0, 0, 0, 0)
        check_col.setSpacing(6)
        self.check_tree = QTreeWidget()
        self.check_tree.setObjectName("checkTree")
        self.check_tree.setHeaderLabels(["상태", "파일 (서버 배포 경로 기준)"])
        self.check_tree.setRootIsDecorated(True)
        self.check_tree.setMaximumHeight(240)
        self.check_tree.header().setStretchLastSection(True)
        check_col.addWidget(self.check_tree)
        self.check_box.hide()
        col.addWidget(self.check_box)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setObjectName("serverLog")
        self.log.setMinimumHeight(220)
        self.log.hide()
        col.addWidget(self.log)
        self.body.addWidget(self.main)
        self.server_combo.currentIndexChanged.connect(self._server_changed)

    # ------------------------------------------------------------------ 상태
    @property
    def project(self) -> Optional[ProjectConfig]:
        return self.result.project if self.result else None

    @property
    def profile(self) -> Optional[ServerProfile]:
        p = self.project
        index = self.server_combo.currentIndex()
        return p.servers[index] if p and 0 <= index < len(p.servers) else None

    def set_package(self, result) -> None:
        """패키지를 만들었을 때: 이 프로젝트의 서버 목록으로 다시 채운다."""
        self.result = result
        servers = result.project.servers if result else []
        self.server_combo.blockSignals(True)
        self.server_combo.clear()
        for s in servers:
            self.server_combo.addItem(f"{s.name}  ·  {s.address}")
        self.server_combo.blockSignals(False)
        self.server_combo.setVisible(bool(servers))
        self.empty_row.setVisible(not servers)
        self.main.setVisible(bool(servers))
        self.set_collapsed(not servers)  # 서버가 없으면 접어 두고, 펼치면 등록 안내가 보인다
        self.log.clear()
        self.log.hide()
        self._server_changed()

    def _server_changed(self, *_args) -> None:
        self.remote_dir = None
        self.show_check(None)
        self._set_stage("ready")
        p = self.profile
        if p is None:
            return
        target = p.server_path or self.project.server_path
        runner = f"{p.runner} 계정으로 실행 ({p.run_with})" if p.run_as else f"{p.user} 계정으로 실행"
        self.target_label.setText(f"올릴 위치 {p.upload_dir}/{self.result.out_dir.name}  ·  반영 경로 {target}  ·  {runner}")

    def _set_stage(self, stage: str, busy: bool = False) -> None:
        self.stage = stage
        self.check_btn.setEnabled(not busy)
        self.apply_btn.setEnabled(not busy and stage == "checked")
        self.rollback_btn.setEnabled(not busy and stage in ("applied", "apply_failed"))
        checked = "확인 끝 · 반영 대기"
        if self.check_result is not None:  # check 결과가 있으면 한 줄에: "확인 끝 · apply 때 바뀌는 파일 3개"
            n = self.check_result.changes_server
            checked = f"확인 끝 · apply 때 바뀌는 파일 {n}개" if n else "확인 끝 · 서버와 모두 같아 바뀌는 파일 없음"
        text, tone = {"ready": ("아직 올리지 않음", "neutral"), "checked": (checked, "success"),
                      "check_failed": ("확인 실패", "danger"), "applied": ("반영 완료", "success"),
                      "apply_failed": ("반영 실패", "danger"), "rolled_back": ("되돌림", "warning")}[stage]
        self.state_chip.set("진행 중…" if busy else text, "info" if busy else tone)
        self.cancel_btn.setVisible(busy)
        self.cancel_btn.setEnabled(True)
        self.cancel_btn.setText("취소")

    def cancel(self) -> None:
        """[취소]: 진행 중인 올리기·실행을 끊는다. 결과는 _run 의 취소 처리로 돌아온다."""
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.setText("중단 중…")
        if not self.client.cancel():   # 비밀번호 입력 등 연결 전 단계면 끊을 것이 없다
            self.cancel_btn.setEnabled(True)
            self.cancel_btn.setText("취소")

    def _append(self, text: str) -> None:
        self.log.moveCursor(self.log.textCursor().MoveOperation.End)
        self.log.insertPlainText(text.replace("\r\n", "\n"))
        self.log.ensureCursorVisible()

    def show_check(self, result: Optional[remote.CheckResult]) -> None:
        """check 로그를 표로: 상태 배지 옆의 상태별 개수 배지와, 상태마다 접을 수 있는 파일 목록."""
        self.check_result = result
        self.check_tree.clear()
        while self.check_chips.count():
            item = self.check_chips.takeAt(0)
            if item.widget():
                item.widget().hide()
                item.widget().deleteLater()
        if result is None:
            self.check_box.hide()
            return
        tones = {"신규": "success", "변경": "warning", "동일": "neutral", "삭제예정": "danger", "이미없음": "neutral",
                 "정리예정": "danger"}
        for kind in remote.CHECK_KINDS:
            count = result.counts.get(kind, 0)
            if not count:
                continue
            self.check_chips.addWidget(Chip(f"{kind} {count}", tones[kind]))
            top = QTreeWidgetItem([f"{kind} {count}", remote.CHECK_TEXT[kind]])
            top.setForeground(0, theme.tone_color(tones[kind]))
            paths = result.paths(kind)
            for path in paths:
                top.addChild(QTreeWidgetItem(["", path]))
            if count > len(paths):  # 파일이 많으면 서버 스크립트가 '동일' 은 개수만 찍는다
                top.addChild(QTreeWidgetItem(["", f"(목록 생략: {count - len(paths)}개)"]))
            self.check_tree.addTopLevelItem(top)
            top.setExpanded(kind != "동일")
        self.check_tree.resizeColumnToContents(0)
        self.check_box.show()

    def _record(self, p: ServerProfile, res: remote.StepResult) -> None:
        """서버에서 한 일을 패키지 기록 파일에 남긴다 (기록 화면의 '서버 반영' 탭, 서버에서 되돌리기)."""
        if self.result is None or self.result.out_dir is None:
            return
        try:
            history.append_deployment(self.result.out_dir, p.name, p.address, res.action, res.code, res.remote_dir,
                                      p.server_path or self.project.server_path)
        except OSError as e:
            self._append(f"\n[경고] 반영 이력을 기록 파일에 남기지 못했습니다: {e}\n")

    def _progress(self, sent: int, total: int) -> None:
        self.progress.show()
        self.progress.setRange(0, total)
        self.progress.setValue(sent)

    # ------------------------------------------------------------------ 동작
    def _run(self, make_step, on_done: Callable[[Any], None]) -> None:
        """서버 지문을 확인하고 비밀번호를 준비한 뒤(필요하면 묻고), make_step(secrets) 로 만든 단계를 실행한다."""
        p = self.profile
        if p is None or self.result is None:
            return
        self.log.show()
        prev = self.stage
        self._set_stage(prev, busy=True)

        def failed(message: str) -> None:
            self.progress.hide()
            self._append(f"\n[오류] {message}\n")
            self._set_stage(prev)
            self.ctx.dialogs.error("서버 반영", message)

        def done(result) -> None:
            self.progress.hide()
            on_done(result)

        def cancelled() -> None:  # 비밀번호 입력을 취소했거나 [취소]로 끊었으면 이전 상태 그대로
            self.progress.hide()
            self._set_stage(prev)

        self.client.start(p, make_step, self._append, self._progress, done, failed, cancelled)

    def upload_and_check(self) -> None:
        p = self.profile
        if p is None:
            return
        self.log.clear()
        self.show_check(None)
        package = self.result.out_dir

        def done(res: remote.StepResult) -> None:
            self.remote_dir = res.remote_dir
            self._record(p, res)
            if res.ok:
                self.show_check(remote.parse_check(self.log.toPlainText()))
                self._append("\n✓ 확인 끝. 위 표를 보고 [백업 후 반영]을 누르세요.\n")
                self._set_stage("checked")
            else:
                self._append(f"\n✗ {res.action} 실패 (종료 코드 {res.code})\n")
                self._set_stage("check_failed")

        self._run(lambda secrets: lambda conn, output, progress: remote.upload_and_check(
            conn, p, secrets, package, output, progress), done)

    def _hooks_answer(self, stages: Tuple[str, ...]) -> bool:
        """서버 명령을 함께 실행할지 묻는다 (명령이 없으면 True)."""
        hooks = {s: c for s, c in (self.result.hooks or {}).items() if s in stages and c}
        if not hooks:
            return True
        lines = "\n".join(f"- {HOOK_TEXT.get(s, s)}: {cmd}" for s, cmds in hooks.items() for cmd in cmds)
        return self.ctx.dialogs.ask("서버 명령", f"이 패키지에는 서버에서 실행할 명령이 있습니다.\n\n{lines}\n\n"
                                               "함께 실행할까요? ([아니요]를 누르면 파일만 반영합니다)")

    def apply(self) -> None:
        p = self.profile
        if p is None or not self.remote_dir:
            return
        target = p.server_path or self.project.server_path
        if not self.ctx.dialogs.ask("반영하기", f"{p.name} 서버({p.address})의 {target} 에 반영합니다.\n"
                                              "먼저 바뀌거나 지워질 기존 파일을 백업한 뒤 반영합니다.\n\n계속할까요?"):
            return
        run_hooks = self._hooks_answer(("pre_apply", "post_apply"))
        remote_dir = self.remote_dir

        def done(res: remote.StepResult) -> None:
            self._record(p, res)
            if res.ok:
                self._append("\n✓ 반영했습니다. 문제가 있으면 [되돌리기]를 누르세요.\n")
                self._set_stage("applied")
            else:
                self._append(f"\n✗ {res.action} 실패 (종료 코드 {res.code})\n")
                self._set_stage("apply_failed" if res.action == "apply" else "checked")

        self._run(lambda secrets: lambda conn, output, progress: remote.backup_and_apply(
            conn, p, secrets, remote_dir, output, run_hooks), done)

    def rollback(self) -> None:
        p = self.profile
        if p is None or not self.remote_dir:
            return
        if not self.ctx.dialogs.ask("되돌리기", f"{p.name} 서버({p.address})를 이번 반영 전으로 되돌릴까요?"):
            return
        run_hooks = self._hooks_answer(("post_rollback",))
        remote_dir = self.remote_dir

        def done(res: remote.StepResult) -> None:
            self._record(p, res)
            self._append("\n✓ 되돌렸습니다.\n" if res.ok else f"\n✗ rollback 실패 (종료 코드 {res.code})\n")
            self._set_stage("rolled_back" if res.ok else "apply_failed")

        self._run(lambda secrets: lambda conn, output, progress: remote.run_action(
            conn, p, secrets, remote_dir, "rollback", output, yes=run_hooks, no_hooks=not run_hooks), done)
