"""서버에 바로 올리고 실행하기 (SSH/SFTP).

1. 처음 접속하는 서버는 지문(host key)을 사용자에게 보여 주고 확인을 받은 뒤 known_hosts 에 기억한다.
   기억한 지문과 다르면 접속하지 않는다 (다른 서버로 연결될 위험).
2. 패키지 폴더를 upload_dir/<패키지 이름> 으로 올린다.
3. 서버 스크립트를 실행 계정으로 실행한다: 접속 계정 그대로 / sudo -u 계정 / su - 계정.
   비밀번호가 필요하면(run_password) sudo 는 표준 입력으로, su 는 가상 터미널의 암호 질문에 답한다.
   비밀번호를 쓰지 않는 설정인데 서버가 비밀번호를 물으면 기다리지 않고 바로 실패한다
   (sudo 는 -n, su 는 암호 질문이 보이는 즉시 중단). 그러지 않으면 명령이 영원히 멈춘다.

화면·명령어와 실제 연결을 떼어 두려고 Connection(올리기·실행) 을 따로 둔다. 테스트는 같은 모양의 로컬 연결을 쓴다.
"""
from __future__ import annotations

import base64
import hashlib
import posixpath
import re
import shlex
import socket
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Protocol, Tuple

from shipkit.config import ServerProfile, user_config_path

Output = Callable[[str], None]
Progress = Callable[[int, int], None]     # (보낸 바이트, 전체 바이트)
EXECUTABLES = ("shipkit.sh",)             # 올린 뒤 실행 권한을 줄 파일 (hooks/*.sh 도)
CONNECT_TIMEOUT = 15
IDLE_TIMEOUT = 600                        # 명령 실행 중 이만큼(초) 서버 출력이 없으면 끊긴 것으로 보고 중단한다
PROMPT_WINDOW = 256                       # 가상 터미널 출력이 이만큼 쌓이도록 암호 질문이 없으면 더 지켜보지 않는다
SUDO_NEEDS_PASSWORD = "a password is required"   # sudo -n 이 비밀번호 없이 실행하지 못할 때 내는 문구


class RemoteError(Exception):
    pass


class RemoteAuthError(RemoteError):
    """로그인 실패 (비밀번호·키가 틀림)."""


class RemoteCancelled(RemoteError):
    """사용자가 [취소]를 눌러 올리기·실행을 중단했다."""


# ---------------------------------------------------------------------- 서버 지문
def known_hosts_path() -> Path:
    return user_config_path().parent / "known_hosts"


def host_label(host: str, port: int) -> str:
    """known_hosts 의 서버 이름 (22 가 아니면 [host]:port)."""
    return host if port == 22 else f"[{host}]:{port}"


@dataclass
class HostKeyStatus:
    state: str          # known(기억한 지문과 같음) / new(처음) / changed(기억한 지문과 다름)
    key_type: str
    fingerprint: str    # SHA256:...
    key: object = None  # paramiko PKey (new 일 때 기억하려고)


def fingerprint(key) -> str:
    return "SHA256:" + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")


def check_host_key(profile: ServerProfile, path: Optional[Path] = None) -> HostKeyStatus:
    """로그인 없이 서버 지문만 받아 기억한 것과 비교한다."""
    import paramiko
    path = path or known_hosts_path()
    try:
        sock = socket.create_connection((profile.host, profile.port), timeout=CONNECT_TIMEOUT)
    except OSError as e:
        raise RemoteError(f"{profile.host}:{profile.port} 에 연결하지 못했습니다: {e}") from e
    transport = paramiko.Transport(sock)
    try:
        transport.start_client(timeout=CONNECT_TIMEOUT)
        key = transport.get_remote_server_key()
    except paramiko.SSHException as e:
        raise RemoteError(f"SSH 연결을 시작하지 못했습니다: {e}") from e
    finally:
        transport.close()
    known = paramiko.HostKeys()
    if path.is_file():
        known.load(str(path))
    saved = known.lookup(host_label(profile.host, profile.port)) or {}
    if key.get_name() in saved:
        state = "known" if saved[key.get_name()] == key else "changed"
    else:
        state = "changed" if saved else "new"
    return HostKeyStatus(state, key.get_name(), fingerprint(key), key)


def remember_host_key(profile: ServerProfile, status: HostKeyStatus, path: Optional[Path] = None) -> None:
    import paramiko
    path = path or known_hosts_path()
    known = paramiko.HostKeys()
    if path.is_file():
        known.load(str(path))
    known.add(host_label(profile.host, profile.port), status.key_type, status.key)
    path.parent.mkdir(parents=True, exist_ok=True)
    known.save(str(path))


# ---------------------------------------------------------------------- 연결
@dataclass
class Secrets:
    login: Optional[str] = None   # 접속 비밀번호 (auth=password) 또는 키 암호 (auth=key)
    run: Optional[str] = None     # 다른 계정으로 실행할 때 sudo·su 비밀번호


class Connection(Protocol):
    def home(self) -> str: ...
    def upload(self, local_dir: Path, remote_dir: str, progress: Optional[Progress] = None) -> None: ...
    def run(self, command: str, output: Output, password: Optional[str] = None, pty: bool = False) -> int: ...
    def cancel(self) -> None: ...   # 다른 스레드에서: 진행 중인 올리기·명령을 끊는다 (RemoteCancelled 로 끝남)
    def close(self) -> None: ...


def key_needs_passphrase(key_file: str) -> bool:
    import paramiko
    try:
        paramiko.PKey.from_path(Path(key_file).expanduser())
    except paramiko.PasswordRequiredException:
        return True
    except FileNotFoundError as e:
        raise RemoteError(f"SSH 키 파일이 없습니다: {key_file}") from e
    except (paramiko.SSHException, ValueError) as e:
        raise RemoteError(f"SSH 키 파일을 읽지 못했습니다 ({key_file}): {e}") from e
    return False


class SSHConnection:
    """paramiko 로 접속한 서버. 서버 지문은 known_hosts 에 있어야 한다 (check_host_key → remember_host_key).

    idle_timeout: 명령 실행 중 출력이 이만큼(초) 없으면 중단. cancel() 은 화면 스레드에서 불러 진행 중인 작업을 끊는다.
    """

    def __init__(self, profile: ServerProfile, secrets: Secrets, known_hosts: Optional[Path] = None,
                 idle_timeout: float = IDLE_TIMEOUT):
        import paramiko
        self.profile = profile
        self.idle_timeout = idle_timeout
        self._cancelled = False
        self._lock = threading.Lock()
        self._chan = None
        self.client = paramiko.SSHClient()
        path = known_hosts or known_hosts_path()
        if path.is_file():
            self.client.load_host_keys(str(path))
        self.client.set_missing_host_key_policy(paramiko.RejectPolicy())
        kwargs = {"password": secrets.login} if profile.auth == "password" else {
            "key_filename": str(Path(profile.key_file).expanduser()), "passphrase": secrets.login}
        try:
            self.client.connect(profile.host, port=profile.port, username=profile.user, timeout=CONNECT_TIMEOUT,
                                allow_agent=False, look_for_keys=False, **kwargs)
        except paramiko.AuthenticationException as e:
            raise RemoteAuthError(f"{profile.address} 로그인에 실패했습니다. 계정·비밀번호(또는 키)를 확인하세요.") from e
        except paramiko.BadHostKeyException as e:
            raise RemoteError(f"{profile.host} 의 서버 지문이 기억한 것과 다릅니다. 접속하지 않았습니다.") from e
        except paramiko.SSHException as e:
            raise RemoteError(f"{profile.address} 접속 실패: {e}") from e
        except OSError as e:
            raise RemoteError(f"{profile.host}:{profile.port} 에 연결하지 못했습니다: {e}") from e
        self._sftp = None

    @property
    def sftp(self):
        if self._sftp is None:
            self._sftp = self.client.open_sftp()
        return self._sftp

    def home(self) -> str:
        return self.sftp.normalize(".")

    def _mkdirs(self, path: str) -> None:
        parts = []
        while path not in ("", "/"):
            try:
                self.sftp.stat(path)
                break
            except IOError:
                parts.append(path)
                path = posixpath.dirname(path)
        for p in reversed(parts):
            self.sftp.mkdir(p)

    def upload(self, local_dir: Path, remote_dir: str, progress: Optional[Progress] = None) -> None:
        files = [f for f in sorted(local_dir.rglob("*")) if f.is_file()]
        total = sum(f.stat().st_size for f in files) or 1
        sent = 0
        try:
            self._mkdirs(remote_dir)
            for f in files:
                self._check_cancelled()
                rel = f.relative_to(local_dir).as_posix()
                target = posixpath.join(remote_dir, rel)
                self._mkdirs(posixpath.dirname(target))
                self.sftp.put(str(f), target)
                if rel in EXECUTABLES or (rel.startswith("hooks/") and rel.endswith(".sh")):
                    self.sftp.chmod(target, 0o755)
                sent += f.stat().st_size
                if progress:
                    progress(sent, total)
        except (IOError, EOFError) as e:
            self._check_cancelled()
            raise RemoteError(f"올리지 못했습니다 ({remote_dir}): {e}") from e

    def run(self, command: str, output: Output, password: Optional[str] = None, pty: bool = False) -> int:
        import paramiko
        self._check_cancelled()
        try:
            return self._run(command, output, password, pty)
        except socket.timeout as e:
            raise RemoteError(f"서버에서 {self.idle_timeout:g}초 동안 아무 출력이 없어 중단했습니다. "
                              "네트워크가 끊겼거나 명령이 멈춘 것일 수 있습니다. 서버에서 직접 상태를 확인하세요.") from e
        except (paramiko.SSHException, OSError, EOFError) as e:
            self._check_cancelled()
            raise RemoteError(f"서버에서 명령을 실행하지 못했습니다: {e}") from e
        finally:
            with self._lock:
                self._chan = None

    def _check_cancelled(self) -> None:
        if self._cancelled:
            raise RemoteCancelled("취소했습니다.")

    def cancel(self) -> None:
        """진행 중인 올리기·명령을 끊는다. 연결 자체를 닫으므로 이 연결은 더 쓸 수 없다."""
        with self._lock:
            self._cancelled = True
            chan = self._chan
        try:
            if chan is not None:
                chan.close()
            transport = self.client.get_transport()
            if transport is not None:
                transport.close()  # 막혀 있는 recv/put 이 바로 깨어난다
        except Exception:  # noqa: BLE001  끊는 중의 오류는 중요하지 않다
            pass

    def _run(self, command: str, output: Output, password: Optional[str], pty: bool) -> int:
        chan = self.client.get_transport().open_session()
        with self._lock:
            self._chan = chan
        try:
            chan.settimeout(self.idle_timeout)
            if pty:
                chan.get_pty(width=200)
            chan.set_combine_stderr(True)
            chan.exec_command(command)
            if password is not None and not pty:  # sudo -S: 첫 줄로 비밀번호
                chan.sendall((password + "\n").encode("utf-8"))
            # 가상 터미널(su)에서는 처음에 나오는 암호 질문을 지켜본다. 비밀번호가 있으면 답하고 질문 글자는
            # 보여 주지 않는다. 없으면 기다리지 않고 바로 중단한다 (안 그러면 영원히 멈춘다).
            answered = not pty
            pending = ""
            while True:
                data = chan.recv(4096)
                if not data:
                    break
                text = data.decode("utf-8", "replace")
                if not answered:
                    pending += text
                    if _is_password_prompt(pending):
                        if password is None:
                            raise RemoteError(
                                f"{self.profile.runner} 계정으로 바꾸는 데 비밀번호가 필요합니다. "
                                "서버 연결 설정에서 '비밀번호 필요(run_password)'를 켜세요.")
                        chan.sendall((password + "\n").encode("utf-8"))
                        answered, pending = True, ""
                    elif len(pending) > PROMPT_WINDOW:  # 질문 없이 출력이 이어지면 평범한 출력으로 본다
                        output(pending)  # 모아 둔 것을 먼저 내보내고 비운다 (순서가 바뀌면 이 조각이 사라진다)
                        answered, pending = True, ""
                    continue
                output(text)
            if pending:
                output(pending)
            self._check_cancelled()  # 취소로 채널이 닫혀 출력이 끝난 경우
            return chan.recv_exit_status()
        finally:
            chan.close()

    def close(self) -> None:
        if self._sftp is not None:
            self._sftp.close()
        self.client.close()


def _is_password_prompt(text: str) -> bool:
    tail = text.strip().lower()
    return tail.endswith(":") and ("password" in tail or "암호" in tail or "비밀번호" in tail)


# ---------------------------------------------------------------------- 명령 만들기
def expand_dir(path: str, home: str) -> str:
    if path == "~":
        return home
    if path.startswith("~/"):
        return posixpath.join(home, path[2:])
    return path


def remote_package_dir(profile: ServerProfile, package_name: str, home: str) -> str:
    return posixpath.join(expand_dir(profile.upload_dir, home), package_name)


def _sudo(password: bool) -> str:
    """비밀번호를 보낼 때는 표준 입력으로 받고(-S), 아니면 묻지 않고 바로 실패하게 한다(-n)."""
    return "sudo -S -p ''" if password else "sudo -n"


def as_runner(profile: ServerProfile, inner: str, password: Optional[bool] = None) -> str:
    """inner(셸 명령)를 실행 계정으로 실행하는 명령. password: sudo 비밀번호를 보내는지 (None = 설정의 run_password)."""
    if not profile.run_as:
        return f"bash -c {shlex.quote(inner)}"
    if profile.run_with == "su":
        return f"su - {shlex.quote(profile.run_as)} -c {shlex.quote(inner)}"
    if password is None:
        password = profile.run_password
    return f"{_sudo(password)} -u {shlex.quote(profile.run_as)} bash -c {shlex.quote(inner)}"


def script_command(profile: ServerProfile, package_dir: str, action: str, yes: bool = False,
                   no_hooks: bool = False, script: str = "shipkit.sh", password: Optional[bool] = None) -> str:
    """패키지 폴더에서 서버 스크립트를 실행하는 명령 (실행 계정으로)."""
    args = [action]
    if profile.server_path:
        args.append(profile.server_path)
    if yes:
        args.append("-y")
    if no_hooks:
        args.append("--no-hooks")
    inner = f"cd {shlex.quote(package_dir)} && bash {script} " + " ".join(shlex.quote(a) for a in args)
    return as_runner(profile, inner, password)


def chown_command(profile: ServerProfile, package_dir: str, password: Optional[bool] = None) -> Optional[str]:
    """올린 패키지 폴더를 실행 계정 소유로 (접속 계정이 root 가 아니면 sudo).
    password: sudo 비밀번호를 보내는지 (None = 설정의 run_password)."""
    if not (profile.chown and profile.run_as):
        return None
    cmd = f"chown -R {shlex.quote(profile.run_as)}: {shlex.quote(package_dir)}"
    if profile.user == "root":
        return cmd
    if password is None:
        password = profile.run_password
    return f"{_sudo(password)} {cmd}"


def run_password(profile: ServerProfile, secrets: Secrets, command_kind: str = "script") -> dict:
    """run() 에 넘길 비밀번호·가상 터미널 옵션. su 는 비밀번호가 없어도 가상 터미널을 써서 암호 질문을 알아챈다."""
    if command_kind == "chown":
        needs = profile.user != "root"
        return {"password": secrets.run if needs and secrets.run else None, "pty": False}
    if not profile.run_as:
        return {"password": None, "pty": False}
    if profile.run_with == "su":
        return {"password": (secrets.run or "") if profile.run_password else None, "pty": True}
    # sudo: 비밀번호가 있으면(설정에서 켰거나, 소유자 변경 때문에 이미 물어봤으면) 보낸다
    password = secrets.run if secrets.run else ("" if profile.run_password else None)
    return {"password": password, "pty": False}


# ---------------------------------------------------------------------- 배포 단계
@dataclass
class StepResult:
    action: str
    code: int
    remote_dir: str

    @property
    def ok(self) -> bool:
        return self.code == 0


def upload_and_check(conn: Connection, profile: ServerProfile, secrets: Secrets, package_dir: Path,
                     output: Output, progress: Optional[Progress] = None) -> StepResult:
    """패키지를 올리고(필요하면 소유자 변경) check 를 실행한다."""
    remote_dir = remote_package_dir(profile, package_dir.name, conn.home())
    output(f"$ 올리기: {package_dir.name} → {profile.address}:{remote_dir}\n")
    conn.upload(package_dir, remote_dir, progress)
    opts = run_password(profile, secrets, "chown")
    chown = chown_command(profile, remote_dir, password=opts["password"] is not None)
    if chown:
        output(f"$ {chown}\n")
        code = _run_sudo_aware(conn, profile, chown, output, opts)
        if code:
            return StepResult("chown", code, remote_dir)
    return run_action(conn, profile, secrets, remote_dir, "check", output)


def run_action(conn: Connection, profile: ServerProfile, secrets: Secrets, remote_dir: str, action: str,
               output: Output, yes: bool = False, no_hooks: bool = False) -> StepResult:
    opts = run_password(profile, secrets)
    cmd = script_command(profile, remote_dir, action, yes=yes, no_hooks=no_hooks, password=opts["password"] is not None)
    output(f"\n$ {action}  ({profile.runner} 계정)\n")
    code = _run_sudo_aware(conn, profile, cmd, output, opts)
    return StepResult(action, code, remote_dir)


def _run_sudo_aware(conn: Connection, profile: ServerProfile, cmd: str, output: Output, opts: dict) -> int:
    """conn.run 과 같되, 비밀번호 없이 sudo -n 을 썼다가 비밀번호가 필요해 실패하면 고칠 방법을 덧붙인다."""
    seen = []

    def out(text: str) -> None:
        seen.append(text)
        output(text)

    code = conn.run(cmd, out, **opts)
    if code and opts["password"] is None and cmd.startswith("sudo -n") and SUDO_NEEDS_PASSWORD in "".join(seen):
        output(f"[안내] {profile.user} 계정의 sudo 에 비밀번호가 필요합니다. "
               "서버 연결 설정에서 '비밀번호 필요(run_password)'를 켜거나, 서버의 sudoers 에 NOPASSWD 를 설정하세요.\n")
    return code


def backup_and_apply(conn: Connection, profile: ServerProfile, secrets: Secrets, remote_dir: str, output: Output,
                     run_hooks: bool) -> StepResult:
    result = run_action(conn, profile, secrets, remote_dir, "backup", output)
    if not result.ok:
        return result
    return run_action(conn, profile, secrets, remote_dir, "apply", output, yes=run_hooks, no_hooks=not run_hooks)


# ---------------------------------------------------------------------- check 출력 읽기
# 서버 스크립트 check 가 찍는 줄: "  [신규] path", "  [변경] path", ... 와 요약 "[정보] 신규 N / 변경 N / ..."
CHECK_KINDS = ("신규", "변경", "동일", "삭제예정", "이미없음", "정리예정")
CHECK_TEXT = {"신규": "서버에 새로 생기는 파일", "변경": "내용이 달라 바뀌는 파일", "동일": "서버와 같아 건드리지 않는 파일",
              "삭제예정": "서버에서 지울 파일", "이미없음": "지우려 했지만 서버에 이미 없는 파일",
              "정리예정": "이번 빌드에 없어 정리 폴더에서 지울 옛 파일"}
_CHECK_ITEM = re.compile(r"^\s*\[(" + "|".join(CHECK_KINDS) + r")\] (.+?)\s*$")
_CHECK_SUMMARY = re.compile(r"\[정보\]\s*(신규 \d+(?: / [^/\n]+?\d+)*)\s*$")
_CHECK_COUNT = re.compile(r"(" + "|".join(CHECK_KINDS) + r") (\d+)")


@dataclass
class CheckResult:
    """check 결과: 상태별 파일 목록과 개수. 파일이 많으면(100개 초과) '동일' 은 개수만 있고 목록에 없다."""
    items: List[Tuple[str, str]] = field(default_factory=list)   # (상태, 서버 경로 기준 상대 경로)
    counts: Dict[str, int] = field(default_factory=dict)

    def paths(self, kind: str) -> List[str]:
        return [p for k, p in self.items if k == kind]

    @property
    def changes_server(self) -> int:
        """apply 때 실제로 서버가 바뀌는 파일 수."""
        return sum(self.counts.get(k, 0) for k in ("신규", "변경", "삭제예정", "정리예정"))


def parse_check(text: str) -> Optional[CheckResult]:
    """check 출력에서 상태별 파일과 요약 개수를 읽는다. check 출력이 아니면 None."""
    result = CheckResult()
    found = False
    for line in text.replace("\r\n", "\n").split("\n"):
        m = _CHECK_ITEM.match(line)
        if m:
            result.items.append((m.group(1), m.group(2)))
            found = True
            continue
        m = _CHECK_SUMMARY.search(line)
        if m:
            result.counts = {k: int(n) for k, n in _CHECK_COUNT.findall(m.group(1))}
            found = True
    if not found:
        return None
    for kind in CHECK_KINDS:  # 요약이 없으면 목록에서 센다
        result.counts.setdefault(kind, len(result.paths(kind)))
    return result


# ---------------------------------------------------------------------- 서버 배포 이력 (history.log)
HISTORY_FIELDS = ("at", "user", "action", "target", "package", "result")


def parse_history(text: str) -> List[dict]:
    """서버 스크립트가 남긴 history.log (탭으로 나뉜 시각·계정·명령·대상·패키지·결과) 를 읽는다. 오래된 것부터."""
    entries = []
    for line in text.replace("\r\n", "\n").split("\n"):
        parts = line.rstrip("\r").split("\t")
        if len(parts) < 5 or not parts[0].strip():
            continue
        parts = (parts + [""] * 6)[:6]
        entries.append(dict(zip(HISTORY_FIELDS, parts)))
    return entries


def current_package(entries: List[dict], target: str) -> Optional[dict]:
    """대상 경로를 마지막으로 바꾼 이력 (apply 또는 rollback). 없으면 None."""
    for e in reversed(entries):
        if e["target"] == target and e["action"] in ("apply", "rollback"):
            return e
    return None


def fetch_history(conn: Connection, profile: ServerProfile, secrets: Secrets, history_file: str,
                  output: Output) -> List[dict]:
    """서버의 배포 이력 파일을 실행 계정으로 읽는다 (이력은 그 계정이 쓴 것이라 '~' 도 같은 집을 가리킨다)."""
    opts = run_password(profile, secrets)
    if history_file.startswith("~/"):  # 서버 스크립트와 같은 규칙으로 실행 계정의 집 폴더
        path = '"$HOME"/' + shlex.quote(history_file[2:])
    else:
        path = shlex.quote(history_file)
    inner = f"cat {path} 2>/dev/null || true"
    lines: List[str] = []
    code = _run_sudo_aware(conn, profile, as_runner(profile, inner, opts["password"] is not None),
                           lines.append, opts)
    if code:
        output("".join(lines))
        raise RemoteError(f"서버 이력을 읽지 못했습니다 (종료 코드 {code}).")
    return parse_history("".join(lines))
