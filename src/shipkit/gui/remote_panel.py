"""서버에 바로 반영 (SSH/SFTP): 배포 완료 화면의 [올리고 확인] → [백업 후 반영] → [되돌리기], 프로젝트 설정의 [연결 시험].

비밀번호는 필요할 때 묻고, [이 PC 에 저장]을 고르면 Windows 자격 증명 관리자에 넣어 다음부터 묻지 않는다.
로그인에 실패하면 저장해 둔 비밀번호를 지워서 다음에 다시 묻게 한다.
처음 접속하는 서버는 지문을 보여 주고 확인을 받는다. 기억한 지문과 다르면 접속하지 않는다.
"""
from __future__ import annotations

import re
import shlex
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QPlainTextEdit, QProgressBar, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from shipkit import history, notify, remote
from shipkit.config import ProjectConfig, ServerProfile
from shipkit.gui import theme
from shipkit.gui.context import AppContext
from shipkit.gui.diff_view import DiffDialog
from shipkit.gui.widgets import Chip, Section, button, label
from shipkit.secrets import SecretError, delete_secret, get_secret, server_secret_key, set_secret

HOOK_TEXT = {"pre_apply": "반영 전", "post_apply": "반영 후", "post_rollback": "되돌린 후"}


_secret_slots = remote.secret_slots   # 어떤 비밀번호가 필요한지는 명령어(shipkit deploy)와 같은 규칙


def notify_messenger(ctx: AppContext, profile: ServerProfile, project: str, package: str, res: remote.StepResult,
                     append: Callable[[str], None], files: Optional[int] = None, package_dir: Optional[Path] = None,
                     health: Optional[remote.HealthResult] = None) -> None:
    """반영·되돌리기가 끝났을 때 서버 연결에 메신저 웹훅이 있으면 결과를 보낸다 (한 줄, 설정에 따라 변경 내역을 아래에).

    package_dir 을 주면 그 패키지 기록(MR·PR 제목, 파일 목록)으로 변경 내역을 만든다 (되돌리기에는 붙이지 않는다).
    백그라운드에서 보내고(메신저가 느려도 화면이 멈추지 않게) 결과는 로그에 한 줄 남긴다. 실패해도 반영 결과는 그대로다."""
    cfg = profile.notify
    if cfg is None:
        return
    details: List[str] = []
    if package_dir is not None and res.action != "rollback" and cfg.detail != "none":
        try:
            details = notify.detail_lines(history.read_record(package_dir), cfg.detail)
        except OSError:
            details = []
    text = notify.compose(project, profile.name, res.action, package, res.ok, res.code, files, details=details, health=health)

    def done(_result) -> None:
        extra = len(text.splitlines()) - 1
        append(f"📣 {cfg.label} 알림을 보냈습니다: {text.splitlines()[0]}" + (f" (+{extra}줄)" if extra else "") + "\n")

    def failed(message: str) -> None:
        append(f"[경고] {message}\n")
        ctx.dialogs.toast(message, "warning")

    ctx.runner.run(lambda: notify.send(cfg, text), done, failed, quiet=True)


def run_health_check(ctx: AppContext, profile: ServerProfile, append: Callable[[str], None],
                     on_done: Callable[[remote.HealthResult], None], is_cancelled: Callable[[], bool]) -> None:
    """반영 뒤 서버 연결의 확인 URL·포트에 이 PC 에서 접속해 본다 (백그라운드, 진행은 로그에). 끝나면 on_done(결과)."""
    cfg = profile.health
    assert cfg is not None
    append(f"\n$ 반영 후 확인: {cfg.target} (최대 {cfg.wait}초, 이 PC 에서 접속)\n")

    def done(result: remote.HealthResult) -> None:
        append(("✓ " if result.ok else "[경고] " if result.cancelled else "✗ ") + result.summary() + "\n")
        on_done(result)

    def failed(message: str) -> None:
        append(f"[경고] 반영 후 확인을 하지 못했습니다: {message}\n")
        on_done(remote.HealthResult(False, message, 0.0, 0, cfg.target))

    ctx.runner.run_stream(lambda emit: remote.health_check(cfg, profile.host, emit, is_cancelled), done, failed, append)


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
        # 이 실행에서 이미 확인한 서버는 다시 묻지 않는다. 단, 그 뒤 [서버 지문 잊기] 등으로 known_hosts 에서 지워졌으면
        # 접속이 거부되므로(RejectPolicy) 처음처럼 다시 확인한다
        if (profile.host, profile.port) in self._trusted:
            if remote.has_host_key(profile):
                then()
                return
            self._trusted.discard((profile.host, profile.port))

        def checked(status: remote.HostKeyStatus) -> None:
            if status.state == "changed":
                failed(f"{profile.host} 의 서버 지문이 예전에 기억한 것과 다릅니다. 다른 서버로 연결될 위험이 있어 "
                       f"접속하지 않았습니다.\n지금 지문: {status.fingerprint}\n서버를 다시 설치한 것이 맞다면 프로젝트 설정 → "
                       "서버 연결의 [서버 지문 잊기]를 누른 뒤 다시 접속하세요.")
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

    def test(self, profile: ServerProfile, done: Callable[[Any], None], failed: Callable[[str], None],
             cancelled: Callable[[], None], server_path: str = "") -> None:
        """[연결 시험]: 리눅스 서버는 remote.diagnose 로 접속 계정·실행 계정 전환·올릴 위치·배포 경로·명령·로그 파일을 확인해
        done(Diagnosis). Windows 서버는 로그인·올릴 위치만 확인해 done(글)."""

        def make_step(secrets):
            if profile.server_os != "windows":
                return lambda conn, output, progress: remote.diagnose(conn, profile, secrets, server_path)
            return lambda conn, output, progress: step(conn, secrets)

        def step(conn, secrets):
            lines: List[str] = []
            opts = remote.run_password(profile, secrets)
            code = conn.run(remote.as_runner(profile, "id -un", opts["password"] is not None), lines.append, **opts)
            who = "".join(lines).strip().splitlines()[-1:] or [""]
            if code or who[0].strip() != profile.runner:
                return False, f"{profile.runner} 계정으로 실행하지 못했습니다 (종료 코드 {code}): {''.join(lines).strip()}"
            streams = remote.stream_upload(profile)   # 실행 계정으로 바로 올리면 올릴 위치도 실행 계정 기준
            home = remote.runner_home(conn, profile, opts) if streams and profile.upload_dir.startswith("~") else conn.home()
            upload = remote.expand_dir(profile.upload_dir, home)
            probe = f"mkdir -p {shlex.quote(upload)} && test -w {shlex.quote(upload)}"
            if streams:
                writable = conn.run(remote.as_runner(profile, probe, opts["password"] is not None), lines.append, **opts) == 0
            else:
                writable = conn.run(probe, lines.append) == 0
            if not writable:
                return False, f"올릴 위치 {upload} 에 쓸 수 없습니다 ({profile.runner if streams else profile.user} 계정)."
            how = f"  ({profile.runner} 계정으로 바로 올림)" if streams else ""
            return True, f"접속 성공: {profile.address}\n실행 계정: {profile.runner}\n올릴 위치: {upload}{how}"

        def finished(result) -> None:
            if isinstance(result, remote.Diagnosis):
                done(result)
                return
            ok, text = result
            (done if ok else failed)(text)

        self.start(profile, make_step, lambda t: None, lambda a, b: None, finished, failed, cancelled)


_STAGE_LINE = re.compile(r"^\$ (.+)$")   # remote.py 가 단계마다 찍는 "$ …" 줄


class ServerLog(QPlainTextEdit):
    """서버 작업 출력 창: 단계 구분선·걸린 시간·실패 강조 (배포 완료 화면과 여러 서버 차례 반영 창이 같이 쓴다).
    출력에서 "$ 올리기: …", "$ check  (계정)" 줄이 단계의 시작이다 (remote.py). 그 줄을 시각이 붙은 구분선으로 바꾸고,
    단계가 끝나면 걸린 시간을 한 줄 덧붙인다. ✓/✗/[오류]/[경고] 줄은 색으로 구분한다."""

    def __init__(self):
        super().__init__()
        self.setReadOnly(True)
        self.setObjectName("serverLog")
        self.reset()

    def reset(self) -> None:
        self.clear()
        self._stage: Optional[Tuple[str, float]] = None   # (이름, 시작 시각 monotonic)
        self._at_line_start = True
        self._line_fmt: Optional[QTextCharFormat] = None

    @staticmethod
    def fmt(color: str, bold: bool = False) -> QTextCharFormat:
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(color))
        if bold:
            fmt.setFontWeight(QFont.Weight.Bold)
        return fmt

    @classmethod
    def _line_format(cls, line: str) -> Optional[QTextCharFormat]:
        s = line.lstrip()
        if s.startswith("✓"):
            return cls.fmt(theme.TONES["success"][0], True)
        if s.startswith(("✗", "[오류]", "[취소]", "[실패]")):
            return cls.fmt(theme.TONES["danger"][0], True)
        if s.startswith(("[경고]", "[안내]")):
            return cls.fmt(theme.TONES["warning"][0])
        return None

    def _insert(self, text: str, fmt: Optional[QTextCharFormat] = None) -> None:
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(text, fmt or QTextCharFormat())
        self.setTextCursor(cursor)
        self.ensureCursorVisible()

    def append(self, text: str) -> None:
        for piece in text.replace("\r\n", "\n").splitlines(keepends=True):
            line = piece.rstrip("\n")
            if self._at_line_start:
                m = _STAGE_LINE.match(line)
                if m:
                    self._stage_header(m.group(1).strip())
                    self._at_line_start = True
                    continue
                self._line_fmt = self._line_format(line)
            self._insert(piece, self._line_fmt)
            self._at_line_start = piece.endswith("\n")

    def banner(self, text: str) -> None:
        """서버가 바뀔 때의 머리줄 (여러 서버 차례 반영)."""
        self.close_stage()
        if not self._at_line_start:
            self._insert("\n")
        self._insert(f"\n━━ {text} ━━━━━━━━━━━━━━━━━━━━━━━━\n", self.fmt(theme.PRIMARY, True))
        self._at_line_start = True

    def _stage_header(self, name: str) -> None:
        self.close_stage()
        self._stage = (name, time.monotonic())
        self._insert(f"── {datetime.now():%H:%M:%S}  {name} ──────────\n", self.fmt(theme.PRIMARY, True))

    def close_stage(self) -> None:
        """단계가 끝났을 때 걸린 시간을 덧붙인다 (다음 단계가 시작되거나 작업이 끝났을 때)."""
        if self._stage is None:
            return
        _name, started = self._stage
        self._stage = None
        if not self._at_line_start:
            self._insert("\n")
            self._at_line_start = True
        self._insert(f"   ⏱ {time.monotonic() - started:.1f}초\n", self.fmt(theme.MUTED))


def ask_hooks(ctx: AppContext, project: ProjectConfig, result, stages: Tuple[str, ...]) -> bool:
    """패키지에 서버 명령(hooks)이 있으면 함께 실행할지 묻는다 (명령이 없으면 True).
    지난 패키지 때와 서버 명령 설정이 달라졌으면 누가 바꿨는지 확인하라고 덧붙인다."""
    hooks = {s: c for s, c in (result.hooks or {}).items() if s in stages and c}
    if not hooks:
        return True
    lines = "\n".join(f"- {HOOK_TEXT.get(s, s)}: {cmd}" for s, cmds in hooks.items() for cmd in cmds)
    prev_name, changed = history.hooks_changed_since(project, exclude=result.out_dir)
    warn = ("\n\n⚠ 지난 패키지 " + prev_name + " 때와 서버 명령 설정이 다릅니다. 누가 바꿨는지 확인하세요:\n"
            + "\n".join(f"  · {c}" for c in changed)) if changed else ""
    return ctx.dialogs.ask("서버 명령", f"이 패키지에는 서버에서 실행할 명령이 있습니다.\n\n{lines}{warn}\n\n"
                                       "함께 실행할까요? ([아니요]를 누르면 파일만 반영합니다)")


def branch_mismatch(result, p: ServerProfile) -> str:
    """서버에 배포 브랜치를 적어 뒀는데 이 패키지를 만든 브랜치가 다르면 그 설명, 아니면 빈 글."""
    if not p.branch or result is None or result.out_dir is None:
        return ""
    record = history.read_record(result.out_dir)
    made = record.git_branch if record else ""
    if not made or made == p.branch:
        return ""
    return f"{p.name} 서버는 {p.branch} 브랜치로 배포하는데, 이 패키지는 {made} 브랜치에서 만들었습니다."


def record_deployment(result, project: ProjectConfig, p: ServerProfile, res: remote.StepResult) -> str:
    """서버에서 한 일을 패키지 기록 파일에 남긴다 (기록 화면의 '서버 반영' 탭, 서버에서 되돌리기). 못 남기면 경고 글."""
    if result is None or result.out_dir is None:
        return ""
    try:
        history.append_deployment(result.out_dir, p.name, p.address, res.action, res.code, res.remote_dir,
                                  p.server_path or project.server_path)
    except OSError as e:
        return f"\n[경고] 반영 이력을 기록 파일에 남기지 못했습니다: {e}\n"
    return ""


class RemotePanel(Section):
    """배포 완료 화면의 '서버에 바로 반영' 구역 (접을 수 있다)."""
    settings_requested = Signal()  # 서버가 없을 때 [서버 등록하러 가기]
    log_requested = Signal(str)    # [로그 보기]: 서버 화면에서 이 서버(이름)의 로그 파일을 따라간다

    def __init__(self, ctx: AppContext):
        self.server_combo = QComboBox()
        self.server_combo.setMinimumWidth(220)
        super().__init__("서버에 바로 반영", right=self.server_combo, rule=True, collapsible=True)
        self.ctx = ctx
        self.client = RemoteClient(ctx)
        self._health_running = False       # 반영 후 확인 중 ([취소]는 이것을 멈춘다)
        self._health_cancel = False
        self.result = None                 # 방금 만든 패키지 (PackageResult)
        self.health_result = None          # 마지막 반영 후 확인 결과 (remote.HealthResult)
        self.remote_dir: Optional[str] = None
        self.stage = "ready"               # ready / checked / applied
        self.batch: Optional[Any] = None   # [여러 서버에 차례로…] 창 (BatchDeployDialog). 패키지를 새로 만들면 버린다

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
        current_row = QHBoxLayout()
        current_row.setSpacing(8)
        self.prod_chip = Chip("운영 서버", "danger")
        self.prod_chip.setToolTip("이름에 운영·상용·prod·live·real 이 들어간 서버는 운영 서버로 보고 반영 전에 한 번 더 확인합니다.")
        self.prod_chip.hide()
        self.branch_chip = Chip("브랜치 다름", "danger")   # 서버의 배포 브랜치와 이 패키지를 만든 브랜치가 다를 때
        self.branch_chip.hide()
        self.current_label = label("", "muted")   # 이 PC 기록으로 보는, 지금 서버에 반영된 패키지
        self.status_btn = button("서버 상태 보기", "ghost")
        self.status_btn.setToolTip("서버의 반영 이력(history)을 읽어 지금 반영된 패키지를 확인합니다.")
        self.status_btn.clicked.connect(self._show_server_status)
        current_row.addWidget(self.prod_chip)
        current_row.addWidget(self.branch_chip)
        current_row.addWidget(self.current_label, 1)
        current_row.addWidget(self.status_btn)
        col.addLayout(current_row)
        buttons = QHBoxLayout()
        # 글자가 길면 세 버튼이 좁은 창(1000px)에 들어가지 않아서, 스크립트 명령 이름은 말풍선으로 뺐다
        self.check_btn = button("올리고 확인", "primary")
        self.check_btn.setToolTip("패키지를 서버에 올린 뒤 서버 스크립트의 check 를 실행합니다 (서버는 바뀌지 않음)")
        self.check_btn.clicked.connect(self.upload_and_check)
        self.apply_btn = button("백업 후 반영")
        self.apply_btn.setToolTip("backup → apply: 바뀌거나 지워질 파일을 먼저 백업하고 반영합니다")
        self.apply_btn.clicked.connect(self.apply)
        self.rollback_btn = button("되돌리기", "danger")
        self.rollback_btn.setToolTip("rollback: 백업 기준으로 반영 전 상태로 복구합니다")
        self.rollback_btn.clicked.connect(self.rollback)
        self.log_btn = button("로그 보기", "ghost")   # 서버 연결에 로그 파일을 적어 둔 서버만 보인다
        self.log_btn.clicked.connect(lambda: self.profile and self.log_requested.emit(self.profile.name))
        self.log_btn.hide()
        self.state_chip = Chip("", "neutral")
        self.cancel_btn = button("취소", "ghost")   # 진행 중일 때만 보인다
        self.cancel_btn.setToolTip("올리기·실행을 중단합니다. 서버에서 이미 시작된 명령은 그대로 끝까지 돌 수 있습니다.")
        self.cancel_btn.clicked.connect(self.cancel)
        self.cancel_btn.hide()
        for b in (self.check_btn, self.apply_btn, self.rollback_btn, self.log_btn):
            buttons.addWidget(b)
        buttons.addStretch(1)
        self.batch_btn = button("여러 서버에 차례로…", "ghost")   # 서버가 둘 이상일 때만
        self.batch_btn.setToolTip("같은 패키지를 고른 서버들에 순서대로 올리고 확인 → 백업 후 반영합니다. 서버마다 결과가 한 줄로 보입니다.")
        self.batch_btn.clicked.connect(self.open_batch)
        buttons.addWidget(self.batch_btn)
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
        self.check_tree.setObjectName("boxed")   # 평평한 구역 안이라 표 자체에 테두리
        self.check_tree.setHeaderLabels(["상태", "파일 (서버 배포 경로 기준)"])
        self.check_tree.setRootIsDecorated(True)
        self.check_tree.setMaximumHeight(240)
        self.check_tree.header().setStretchLastSection(True)
        self.check_tree.itemSelectionChanged.connect(self._update_diff_btn)
        self.check_tree.itemDoubleClicked.connect(lambda _item, _col: self.show_diff())
        check_col.addWidget(self.check_tree)
        diff_row = QHBoxLayout()
        diff_row.setSpacing(8)
        self.diff_btn = button("차이 보기", "ghost")
        self.diff_btn.setToolTip("고른 파일의 지금 서버 내용과 패키지 내용을 줄 단위로 비교합니다 (변경·신규·삭제예정, 더블클릭도 됨)")
        self.diff_btn.setEnabled(False)
        self.diff_btn.clicked.connect(self.show_diff)
        diff_row.addWidget(self.diff_btn)
        diff_row.addWidget(label("표의 파일을 골라 반영하면 무엇이 바뀌는지 미리 볼 수 있습니다.", "muted"), 1)
        check_col.addLayout(diff_row)
        self.diff_dialog: Optional[DiffDialog] = None
        self.check_box.hide()
        col.addWidget(self.check_box)
        self.log = ServerLog()
        self.log.setMinimumHeight(220)
        self.log.hide()
        col.addWidget(self.log)
        self._reset_log()
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
        self.batch_btn.setVisible(len(servers) >= 2)
        if self.batch is not None and not self.batch._busy:
            self.batch.close()
            self.batch.deleteLater()
            self.batch = None
        self.set_collapsed(not servers)  # 서버가 없으면 접어 두고, 펼치면 등록 안내가 보인다
        self._reset_log()
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
        if remote.stream_upload(p):
            runner = f"{p.runner} 계정으로 올리고 실행 (sudo)"
        elif p.run_as:
            runner = f"{p.runner} 계정으로 실행 ({p.run_with})"
        else:
            runner = f"{p.user} 계정으로 실행"
        self.target_label.setText(f"올릴 위치 {p.upload_dir}/{self.result.out_dir.name}  ·  반영 경로 {target}  ·  {runner}")
        self.prod_chip.setVisible(p.is_production)
        self.log_btn.setVisible(bool(p.log_file))
        self.log_btn.setToolTip(f"서버 화면으로 가서 터미널에서 {p.log_file} 을 tail -F 로 따라갑니다")
        mismatch = self._branch_mismatch(p)
        self.branch_chip.setVisible(bool(mismatch))
        self.branch_chip.setToolTip(mismatch)
        self.refresh_current()

    def _branch_mismatch(self, p) -> str:
        """서버에 배포 브랜치를 적어 뒀는데 이 패키지를 만든 브랜치가 다르면 그 설명, 아니면 빈 글."""
        return branch_mismatch(self.result, p)

    def refresh_current(self) -> None:
        """'지금 이 서버에는 어떤 패키지가 반영돼 있나' 를 이 PC 의 패키지 기록으로 보여 준다 (서버 접속 없이)."""
        p = self.profile
        if p is None or self.project is None:
            self.current_label.setText("")
            return
        try:
            found = history.current_on_server(self.project, p.name)
        except OSError:
            found = None
        if found is None:
            self.current_label.setText(f"{p.name} 서버에 반영한 기록이 이 PC 에는 없습니다.")
            return
        record, state = found
        when = str(state.get("at", ""))[:16].replace("T", " ")
        self.current_label.setText(f"지금 {p.name} 서버에는 {record.name} 패키지가 반영돼 있습니다 ({when}, 이 PC 기록 기준)")

    def _notify_background(self, title: str, text: str) -> None:
        """창이 뒤에 있으면(다른 일을 하는 중) Windows 알림으로 끝났음을 알린다."""
        window = self.window()
        if window is not None and not window.isActiveWindow():
            self.ctx.notify(title, text)

    def open_batch(self) -> None:
        """[여러 서버에 차례로…]: 이 패키지의 일괄 반영 창 (닫았다 열어도 상태와 로그가 남는다)."""
        if self.result is None:
            return
        if self.batch is None:
            from shipkit.gui.batch_deploy import BatchDeployDialog
            self.batch = BatchDeployDialog(self.ctx, self.result, self.window(), on_finished=self.refresh_current)
        self.batch.show()
        self.batch.raise_()
        self.batch.activateWindow()

    def _show_server_status(self) -> None:
        from shipkit.gui.server_status import ServerStatusDialog
        if self.project is None or not self.project.servers:
            return
        ServerStatusDialog(self.ctx, self.project, self, client=self.client).show()

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
        self._update_diff_btn()

    def cancel(self) -> None:
        """[취소]: 진행 중인 올리기·실행을 끊는다. 결과는 _run 의 취소 처리로 돌아온다. 반영 후 확인 중이면 확인만 멈춘다."""
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.setText("중단 중…")
        if self._health_running:
            self._health_cancel = True
            return
        if not self.client.cancel():   # 비밀번호 입력 등 연결 전 단계면 끊을 것이 없다
            self.cancel_btn.setEnabled(True)
            self.cancel_btn.setText("취소")

    # ------------------------------------------------------------------ 로그 (ServerLog: 단계 구분선·걸린 시간·실패 강조)
    def _reset_log(self) -> None:
        self.log.reset()

    def _append(self, text: str) -> None:
        self.log.append(text)

    def _close_stage(self) -> None:
        self.log.close_stage()

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
                child = QTreeWidgetItem(["", path])
                child.setData(0, Qt.ItemDataRole.UserRole, (kind, path))
                top.addChild(child)
            if count > len(paths):  # 파일이 많으면 서버 스크립트가 '동일' 은 개수만 찍는다
                top.addChild(QTreeWidgetItem(["", f"(목록 생략: {count - len(paths)}개)"]))
            self.check_tree.addTopLevelItem(top)
            top.setExpanded(kind != "동일")
        self.check_tree.resizeColumnToContents(0)
        self.check_box.show()
        self._update_diff_btn()

    # ------------------------------------------------------------------ 차이 보기
    DIFF_KINDS = ("변경", "신규", "삭제예정")

    def _picked_check_file(self) -> Optional[Tuple[str, str]]:
        items = self.check_tree.selectedItems()
        data = items[0].data(0, Qt.ItemDataRole.UserRole) if items else None
        return data if isinstance(data, tuple) and data[0] in self.DIFF_KINDS else None

    def _update_diff_btn(self) -> None:
        self.diff_btn.setEnabled(self._picked_check_file() is not None and self.result is not None and not self.busy)

    @property
    def busy(self) -> bool:
        return self.cancel_btn.isVisibleTo(self)

    def show_diff(self) -> None:
        """[차이 보기] / 표의 파일 더블클릭: 서버 파일(-)과 패키지 파일(+)을 비교하는 창."""
        picked, p = self._picked_check_file(), self.profile
        if picked is None or p is None or self.result is None or self.busy:
            return
        kind, rel = picked
        target = (p.server_path or self.project.server_path).rstrip("/")
        server_path = f"{target}/{rel}"
        encodings = list(self.project.source_encodings or ["utf-8", "cp949"])
        package_file = self.result.out_dir / "files" / rel
        package_data = b""
        if kind != "삭제예정":
            try:
                package_data = package_file.read_bytes()
            except OSError as e:
                self.ctx.dialogs.error("차이 보기", f"패키지 파일을 읽지 못했습니다: {e}")
                return
            if b"\x00" in package_data:
                self.ctx.dialogs.error("차이 보기", f"{rel} 은 이진 파일이라 비교하지 않습니다.")
                return
        if kind == "신규":
            self._open_diff(rel, b"", package_data, encodings, kind)
            return

        prev = self.stage

        def done(server_data: bytes) -> None:
            self._set_stage(prev)            # 읽기는 단계를 바꾸지 않는다 (_run 의 '진행 중' 만 풀기)
            self._open_diff(rel, server_data, package_data, encodings, kind)

        self._append(f"\n$ 서버 파일 읽기 (차이 보기): {server_path}\n")
        self._run(lambda secrets: lambda conn, output, progress: remote.read_server_file(conn, p, secrets, server_path), done)

    def _open_diff(self, rel: str, server_data: bytes, package_data: bytes, encodings: List[str], kind: str) -> None:
        old, old_enc, old_nl = remote.decode_text(server_data, encodings) if server_data else ("", "", "")
        new, new_enc, new_nl = remote.decode_text(package_data, encodings) if package_data else ("", "", "")
        notes = []
        if kind == "신규":
            notes.append("서버에 없는 새 파일입니다. 패키지 내용 전체가 생깁니다.")
        elif kind == "삭제예정":
            notes.append("반영하면 서버에서 지워지는 파일입니다. 지금 서버 내용 전체가 없어집니다.")
        if old_nl and new_nl and old_nl != new_nl:
            notes.append(f"줄 끝이 다릅니다: 서버 {'CRLF' if old_nl == chr(13) + chr(10) else 'LF'} · 패키지 "
                         f"{'CRLF' if new_nl == chr(13) + chr(10) else 'LF'} (반영하면 패키지 쪽으로 바뀝니다. 아래 비교에서는 뺐습니다)")
        if old_enc and new_enc and old_enc != new_enc:
            notes.append(f"인코딩이 다릅니다: 서버 {old_enc} · 패키지 {new_enc}")
        if self.diff_dialog is not None:
            self.diff_dialog.close()
        self.diff_dialog = DiffDialog(rel, old, new, note="  ".join(notes), parent=self.window())
        self.diff_dialog.show()

    def _record(self, p: ServerProfile, res: remote.StepResult) -> None:
        """서버에서 한 일을 패키지 기록 파일에 남긴다 (기록 화면의 '서버 반영' 탭, 서버에서 되돌리기)."""
        warning = record_deployment(self.result, self.project, p, res)
        if warning:
            self._append(warning)
        self.refresh_current()

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
            self._close_stage()
            self._append(f"\n[오류] {message}\n")
            self._set_stage(prev)
            self.ctx.dialogs.error("서버 반영", message)

        def done(result) -> None:
            self.progress.hide()
            self._close_stage()
            on_done(result)

        def cancelled() -> None:  # 비밀번호 입력을 취소했거나 [취소]로 끊었으면 이전 상태 그대로
            self.progress.hide()
            self._close_stage()
            self._set_stage(prev)

        self.client.start(p, make_step, self._append, self._progress, done, failed, cancelled)

    def upload_and_check(self) -> None:
        p = self.profile
        if p is None:
            return
        mismatch = self._branch_mismatch(p)
        if mismatch and not self.ctx.dialogs.ask("브랜치 확인", mismatch + "\n\n그래도 이 패키지를 올릴까요?"):
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
        return ask_hooks(self.ctx, self.project, self.result, stages)

    def apply(self) -> None:
        p = self.profile
        if p is None or not self.remote_dir:
            return
        target = p.server_path or self.project.server_path
        warn = "⚠ 운영 서버입니다. 사용자에게 바로 영향이 갑니다.\n\n" if p.is_production else ""
        if not self.ctx.dialogs.ask("운영 서버에 반영하기" if p.is_production else "반영하기",
                                    f"{warn}{p.name} 서버({p.address})의 {target} 에 반영합니다.\n"
                                    "먼저 바뀌거나 지워질 기존 파일을 백업한 뒤 반영합니다.\n\n계속할까요?"):
            return
        run_hooks = self._hooks_answer(("pre_apply", "post_apply"))
        remote_dir = self.remote_dir

        def done(res: remote.StepResult) -> None:
            self._record(p, res)
            if res.ok:
                self._append("\n✓ 반영했습니다. 문제가 있으면 [되돌리기]를 누르세요."
                             + (" 서버 로그는 [로그 보기].\n" if p.log_file else "\n"))
                self._set_stage("applied")
            else:
                self._append(f"\n✗ {res.action} 실패 (종료 코드 {res.code})\n")
                self._set_stage("apply_failed" if res.action == "apply" else "checked")
            self._notify_background(f"{self.project.name} → {p.name} 반영 " + ("완료" if res.ok else f"실패 ({res.action})"),
                                    f"패키지 {self.result.out_dir.name}")
            if res.ok and p.health:
                self._health_check(p, res)          # 확인이 끝난 뒤 결과와 함께 알린다
            else:
                self._notify_apply(p, res)

        self._run(lambda secrets: lambda conn, output, progress: remote.backup_and_apply(
            conn, p, secrets, remote_dir, output, run_hooks), done)

    def _notify_apply(self, p: ServerProfile, res: remote.StepResult, health: Optional[remote.HealthResult] = None) -> None:
        notify_messenger(self.ctx, p, self.project.name, self.result.out_dir.name, res, self._append,
                         files=len(self.result.manifest) + len(self.result.deletes), package_dir=self.result.out_dir,
                         health=health)

    def _health_check(self, p: ServerProfile, res: remote.StepResult) -> None:
        """반영 뒤 서비스 확인 (서버 연결의 URL·포트). 실패하면 되돌릴지 묻는다."""
        self._health_running, self._health_cancel = True, False
        self._set_stage("applied", busy=True)
        self.state_chip.set("반영 완료 · 확인 중…", "info")

        def done(result: remote.HealthResult) -> None:
            self._health_running = False
            self._close_stage()
            self._set_stage("applied")
            self.health_result = result
            self._notify_apply(p, res, result)
            if result.ok:
                self.state_chip.set("반영 완료 · 확인 ✓", "success")
                return
            if result.cancelled:
                return
            self.state_chip.set("반영 완료 · 확인 실패", "danger")
            self._notify_background(f"{self.project.name} → {p.name} 반영 후 확인 실패", result.summary())
            if self.ctx.dialogs.ask("반영 후 확인 실패",
                                    f"{p.name} 서버에 반영은 됐지만 서비스 확인이 실패했습니다.\n\n{result.summary()}\n\n"
                                    "서버 로그를 보고 판단하세요. 지금 바로 이번 반영 전으로 되돌릴까요?"):
                self.rollback(confirm=False)

        run_health_check(self.ctx, p, self._append, done, lambda: self._health_cancel)

    def rollback(self, confirm: bool = True) -> None:
        p = self.profile
        if p is None or not self.remote_dir:
            return
        warn = "⚠ 운영 서버입니다.\n\n" if p.is_production else ""
        if confirm and not self.ctx.dialogs.ask("운영 서버 되돌리기" if p.is_production else "되돌리기",
                                                f"{warn}{p.name} 서버({p.address})를 이번 반영 전으로 되돌릴까요?"):
            return
        run_hooks = self._hooks_answer(("post_rollback",))
        remote_dir = self.remote_dir

        def done(res: remote.StepResult) -> None:
            self._record(p, res)
            self._append("\n✓ 되돌렸습니다.\n" if res.ok else f"\n✗ rollback 실패 (종료 코드 {res.code})\n")
            self._set_stage("rolled_back" if res.ok else "apply_failed")
            self._notify_background(f"{self.project.name} → {p.name} 되돌리기 " + ("완료" if res.ok else "실패"),
                                    f"패키지 {self.result.out_dir.name}")
            notify_messenger(self.ctx, p, self.project.name, self.result.out_dir.name, res, self._append)

        self._run(lambda secrets: lambda conn, output, progress: remote.run_action(
            conn, p, secrets, remote_dir, "rollback", output, yes=run_hooks, no_hooks=not run_hooks), done)
