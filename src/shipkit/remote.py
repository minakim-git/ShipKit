"""서버에 바로 올리고 실행하기 (SSH/SFTP).

1. 처음 접속하는 서버는 지문(host key)을 사용자에게 보여 주고 확인을 받은 뒤 known_hosts 에 기억한다.
   기억한 지문과 다르면 접속하지 않는다 (다른 서버로 연결될 위험).
2. 패키지 폴더를 upload_dir/<패키지 이름> 으로 올린다.
3. 서버 스크립트를 실행 계정으로 실행한다: 접속 계정 그대로 / sudo -u 계정 / su - 계정.
   비밀번호가 필요하면(run_password) sudo 는 표준 입력으로, su 는 가상 터미널의 암호 질문에 답한다.
   비밀번호를 쓰지 않는 설정인데 서버가 비밀번호를 물으면 기다리지 않고 바로 실패한다
   (sudo 는 -n, su 는 암호 질문이 보이는 즉시 중단). 그러지 않으면 명령이 영원히 멈춘다.
   su 의 암호 질문은 서버 로케일에 따라 번역되므로 su 를 LC_ALL=C 로 띄워 영어 "Password:" 가 나오게 하고,
   그래도 번역된 질문이 오면(LC_ALL 을 무시하는 su) 알려진 번역 단어로도 알아챈다.

화면·명령어와 실제 연결을 떼어 두려고 Connection(올리기·실행) 을 따로 둔다. 테스트는 같은 모양의 로컬 연결을 쓴다.
"""
from __future__ import annotations

import base64
import hashlib
import os
import posixpath
import re
import shlex
import socket
import stat
import tarfile
import tempfile
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Protocol, Sequence, Tuple

from shipkit import SCRIPT_NAME, SCRIPT_NAME_PS
from shipkit.config import ServerProfile, user_config_path

Output = Callable[[str], None]
Progress = Callable[[int, int], None]     # (보낸 바이트, 전체 바이트)
EXECUTABLES = (SCRIPT_NAME,)              # 올린 뒤 실행 권한을 줄 파일 (hooks/*.sh 도) - 리눅스 서버만
_DRIVE_ROOT = re.compile(r"^/?[A-Za-z]:$")  # Windows OpenSSH 의 SFTP 경로 (/C:/Users/x 또는 C:/Users/x) 의 드라이브 루트
CONNECT_TIMEOUT = 15
IDLE_TIMEOUT = 600                        # 명령 실행 중 이만큼(초) 서버 출력이 없으면 끊긴 것으로 보고 중단한다
PROMPT_WINDOW = 256                       # 가상 터미널 출력이 이만큼 쌓이도록 암호 질문이 없으면 더 지켜보지 않는다
SUDO_NEEDS_PASSWORD = "a password is required"   # sudo -n 이 비밀번호 없이 실행하지 못할 때 내는 문구 (영어 로케일)
# su·PAM 의 암호 질문에 나오는 단어 (로케일별 번역). 질문은 보통 "Password:" 처럼 콜론으로 끝난다.
PASSWORD_WORDS = ("password", "암호", "비밀번호", "パスワード", "密码", "密碼", "passwort", "contraseña", "mot de passe",
                  "senha", "пароль", "wachtwoord", "hasło", "heslo", "lösenord", "adgangskode", "passord", "şifre",
                  "parola", "jelszó", "salasana")


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


def forget_host_key(profile: ServerProfile, path: Optional[Path] = None) -> bool:
    """기억한 지문을 지운다 (서버를 다시 설치해 지문이 바뀌었을 때). 반환: 지운 것이 있었는지."""
    import paramiko
    path = path or known_hosts_path()
    if not path.is_file():
        return False
    known = paramiko.HostKeys()
    known.load(str(path))
    label = host_label(profile.host, profile.port)
    if not known.lookup(label):
        return False
    del known[label]
    known.save(str(path))
    return True


def has_host_key(profile: ServerProfile, path: Optional[Path] = None) -> bool:
    """이 서버의 지문을 기억하고 있는지 (known_hosts 파일 기준)."""
    import paramiko
    path = path or known_hosts_path()
    if not path.is_file():
        return False
    known = paramiko.HostKeys()
    known.load(str(path))
    return bool(known.lookup(host_label(profile.host, profile.port)))
    return True


# ---------------------------------------------------------------------- 서버 파일 (탐색 화면)
@dataclass(frozen=True)
class RemoteEntry:
    """서버 폴더 안의 항목 하나 (탐색 화면의 한 줄)."""
    name: str
    path: str          # 서버의 절대 경로
    is_dir: bool
    size: int = 0
    mtime: float = 0.0  # epoch 초 (모르면 0)
    link: bool = False  # 심볼릭 링크


SHELL_TAG_VAR = "LC_SHIPKIT_SESSION"   # 셸을 열 때 심는 표식 (sshd 기본 AcceptEnv 가 LC_* 를 받는다. RHEL 계열은 XMODIFIERS 로)

# 서버 셸의 현재 폴더 읽기 (서버 화면의 '터미널 따라가기'). 셸 자체는 건드리지 않고 별도 명령 채널에서 /proc 을 본다.
# 1) 표식(환경 변수)이 든 프로세스 중 부모가 그 묶음에 없는 것이 셸(R). 못 찾으면 내 계정의 셸 중 부모가 sshd 인 가장 최근 것.
# 2) 셸의 전경 프로세스 그룹(tpgid)의 cwd, 없으면 셸의 cwd. 한 번 찾은 PID 는 다음부터 건네받아 다시 훑지 않는다.
CWD_SCRIPT = r"""# shipkit-cwd
T=%(tag)s; R=%(pid)d
if [ "$R" -le 0 ] || [ ! -d "/proc/$R" ]; then
 R=0; M=''
 for d in /proc/[0-9]*; do [ -r "$d/environ" ] || continue; tr '\0' '\n' < "$d/environ" 2>/dev/null | grep -qx -e "LC_SHIPKIT_SESSION=$T" -e "XMODIFIERS=@im=shipkit$T" && M="$M ${d#/proc/}"; done
 for p in $M; do pp=$(sed 's/^.*) //' "/proc/$p/stat" 2>/dev/null | cut -d' ' -f2); case " $M " in *" $pp "*) ;; *) R=$p; break;; esac; done
 if [ "$R" -le 0 ]; then
  R=$(ps -eo pid=,ppid=,uid=,tty=,etimes=,comm= 2>/dev/null | awk -v u="$(id -u)" '{c[$1]=$6} $3==u && $4!="?" && $6 ~ /^-?(bash|sh|zsh|ksh|dash)$/ {n++; p[n]=$1; pp[n]=$2; e[n]=$5} END{best=0; for(i=1;i<=n;i++) if (c[pp[i]] ~ /^sshd/ && (best==0 || e[i]<be)) {best=p[i]; be=e[i]} print best}')
 fi
fi
[ -n "$R" ] || R=0
c=''
if [ "$R" -gt 0 ]; then
 tp=$(sed 's/^.*) //' "/proc/$R/stat" 2>/dev/null | cut -d' ' -f6)
 [ -n "$tp" ] && [ "$tp" -gt 0 ] && c=$(readlink "/proc/$tp/cwd" 2>/dev/null)
 [ -z "$c" ] && c=$(readlink "/proc/$R/cwd" 2>/dev/null)
fi
echo "R $R"; echo "C $c"
"""


def parse_cwd_output(text: str) -> Tuple[int, str]:
    """CWD_SCRIPT 의 출력 → (셸 PID, 현재 폴더). 못 찾았으면 (0, '')."""
    pid, cwd = 0, ""
    for line in text.splitlines():
        if line.startswith("R "):
            try:
                pid = int(line[2:].strip() or 0)
            except ValueError:
                pid = 0
        elif line.startswith("C "):
            cwd = line[2:].strip()
    return pid, cwd


class Shell:
    """대화형 셸(pty) 채널. 읽기 스레드가 받은 바이트를 on_data 로 넘기고, 셸이 끝나면 on_closed 를 부른다.
    (둘 다 읽기 스레드에서 불리므로 화면 쪽은 Qt 신호로 받는다.)"""

    def __init__(self, chan, on_data: Callable[[bytes], None], on_closed: Callable[[], None]):
        self.chan = chan
        self._on_data = on_data
        self._on_closed = on_closed
        self._closed = False
        threading.Thread(target=self._reader, daemon=True, name="shipkit-shell").start()

    def _reader(self) -> None:
        try:
            while True:
                data = self.chan.recv(65536)
                if not data:
                    break
                self._on_data(data)
        except Exception:  # noqa: BLE001  끊기면(취소·네트워크) 그냥 끝난 것으로
            pass
        finally:
            self._closed = True
            self._on_closed()

    @property
    def alive(self) -> bool:
        return not self._closed and not self.chan.closed

    def send(self, data: bytes) -> None:
        if self._closed or not data:
            return
        try:
            self.chan.sendall(data)
        except (OSError, EOFError):
            pass

    def resize(self, cols: int, rows: int) -> None:
        if self._closed:
            return
        try:
            self.chan.resize_pty(width=cols, height=rows)
        except (OSError, EOFError):
            pass

    def close(self) -> None:
        self._closed = True
        try:
            self.chan.close()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------- 연결
@dataclass
class Secrets:
    login: Optional[str] = None   # 접속 비밀번호 (auth=password) 또는 키 암호 (auth=key)
    run: Optional[str] = None     # 다른 계정으로 실행할 때 sudo·su 비밀번호


def secret_slots(profile: ServerProfile) -> List[Tuple[str, str, str]]:
    """이 서버에 필요한 비밀번호: (용도 login/run, 키링 키, 물어볼 문구). 화면과 명령어가 같은 키로 저장·조회한다."""
    from shipkit.secrets import server_secret_key
    slots = []
    p = profile
    if p.auth == "password":
        slots.append(("login", server_secret_key("login", p.user, p.host, p.port), f"{p.address} 접속 비밀번호"))
    elif p.key_file and key_needs_passphrase(p.key_file):
        slots.append(("login", server_secret_key("passphrase", p.user, p.host, p.port),
                      f"SSH 키 암호 ({p.key_file})"))
    # 비밀번호는 설정에서 켰을 때만 묻는다. 소유자 변경(chown)의 sudo 도 다른 명령과 같이 sudo -n 으로 먼저 해 보고,
    # 서버가 비밀번호를 요구하면 그때 안내한다 (sudoers 에 NOPASSWD 가 흔하므로 미리 묻지 않는다)
    needs_run = p.run_as and p.run_password
    if needs_run:
        if p.run_with == "su" and p.run_password:
            key, text = server_secret_key(f"su:{p.run_as}", p.user, p.host, p.port), f"{p.run_as} 계정 비밀번호 (su)"
        else:
            key, text = server_secret_key("sudo", p.user, p.host, p.port), f"{p.user} 계정의 sudo 비밀번호"
        slots.append(("run", key, text))
    return slots


class Connection(Protocol):
    def home(self) -> str: ...
    def upload(self, local_dir: Path, remote_dir: str, progress: Optional[Progress] = None) -> None: ...
    def upload_stream(self, command: str, local_dir: Path, output: Output, progress: Optional[Progress] = None,
                      password: Optional[str] = None) -> int: ...   # local_dir 를 tar.gz 로 묶어 command 의 표준 입력으로
    def run(self, command: str, output: Output, password: Optional[str] = None, pty: bool = False) -> int: ...
    def cancel(self) -> None: ...   # 다른 스레드에서: 진행 중인 올리기·명령을 끊는다 (RemoteCancelled 로 끝남)
    def close(self) -> None: ...


def key_needs_passphrase(key_file: str) -> bool:
    """SSH 개인 키 파일이 암호로 잠겨 있는지. 없거나 읽을 수 없으면 RemoteError."""
    import paramiko
    try:
        paramiko.PKey.from_path(Path(key_file).expanduser())
    except paramiko.PasswordRequiredException:
        return True
    except TypeError as e:
        # paramiko 3.2+ 는 OpenSSH 형식 키를 cryptography 로 읽는데, 암호가 걸려 있으면 PasswordRequiredException 대신
        # TypeError("Key is password-protected, but password was not provided.") 가 난다 (paramiko 5.1 까지 확인)
        if "password" in str(e).lower():
            return True
        raise RemoteError(f"SSH 키 파일을 읽지 못했습니다 ({key_file}): {e}") from e
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
        while path not in ("", "/") and not _DRIVE_ROOT.match(path):
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
                if self.profile.server_os != "windows" and (
                        rel in EXECUTABLES or (rel.startswith("hooks/") and rel.endswith(".sh"))):
                    self.sftp.chmod(target, 0o755)
                sent += f.stat().st_size
                if progress:
                    progress(sent, total)
        except (IOError, EOFError) as e:
            self._check_cancelled()
            raise RemoteError(f"올리지 못했습니다 ({remote_dir}): {e}") from e

    def upload_stream(self, command: str, local_dir: Path, output: Output, progress: Optional[Progress] = None,
                      password: Optional[str] = None) -> int:
        """local_dir 를 tar.gz 로 묶어 command(서버에서 'tar xzf -' 로 받는 명령)의 표준 입력으로 보낸다.
        sudo -S 면 비밀번호를 첫 줄로 먼저 보낸다. 서버 출력은 output 으로 보내고 명령 종료 코드를 돌려준다.
        SFTP 는 접속 계정으로만 되므로, 실행 계정으로 바로 올릴 때 쓴다 (stream_upload)."""
        archive = package_archive(local_dir)
        try:
            total = archive.stat().st_size or 1
            chan = self.client.get_transport().open_session()
            with self._lock:
                self._chan = chan
            try:
                chan.settimeout(self.idle_timeout)
                chan.set_combine_stderr(True)
                chan.exec_command(command)
                try:
                    if password is not None:
                        chan.sendall((password + "\n").encode("utf-8"))
                    sent = 0
                    with archive.open("rb") as f:
                        while True:
                            self._check_cancelled()
                            chunk = f.read(64 * 1024)
                            if not chunk:
                                break
                            chan.sendall(chunk)
                            sent += len(chunk)
                            if progress:
                                progress(sent, total)
                    chan.shutdown_write()
                except (OSError, EOFError):
                    # 서버가 먼저 끊은 경우 (sudo 거부, tar 없음 등): 남은 출력과 종료 코드로 이유를 알린다
                    self._check_cancelled()
                while True:
                    data = chan.recv(4096)
                    if not data:
                        break
                    output(data.decode("utf-8", "replace"))
                self._check_cancelled()
                code = chan.recv_exit_status()
                if code < 0:
                    raise RemoteError("올리는 중 서버와의 연결이 끊겼습니다.")
                return code
            except socket.timeout as e:
                self._check_cancelled()
                raise RemoteError(f"{int(self.idle_timeout)}초 동안 서버 응답이 없어 올리기를 중단했습니다.") from e
            except (IOError, EOFError) as e:
                self._check_cancelled()
                raise RemoteError(f"올리지 못했습니다: {e}") from e
            finally:
                chan.close()
        finally:
            archive.unlink(missing_ok=True)

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

    # ------------------------------------------------------------------ 서버 파일 탐색 (SFTP)
    def _sftp_call(self, what: str, fn, *args):
        """SFTP 호출을 감싸 끊김·권한 오류를 RemoteError 로 바꾼다."""
        self._check_cancelled()
        try:
            return fn(*args)
        except PermissionError as e:
            raise RemoteError(f"{what}: 권한이 없습니다 ({e})") from e
        except FileNotFoundError as e:
            raise RemoteError(f"{what}: 서버에 그런 경로가 없습니다 ({e})") from e
        except (IOError, EOFError, OSError) as e:
            self._check_cancelled()
            raise RemoteError(f"{what}: {e}") from e

    def resolve(self, path: str) -> str:
        """'~', 상대 경로를 서버의 절대 경로로 (존재해야 한다)."""
        if path.startswith("~"):
            path = self.home() + path[1:]
        return self._sftp_call(f"경로 확인 ({path})", self.sftp.normalize, path or ".")

    def is_dir(self, path: str) -> bool:
        attrs = self._sftp_call(f"경로 확인 ({path})", self.sftp.stat, path)
        return stat.S_ISDIR(attrs.st_mode or 0)

    def listdir(self, path: str) -> List[RemoteEntry]:
        """폴더 내용 (폴더 먼저, 이름순). 심볼릭 링크는 가리키는 쪽이 폴더인지 따라가 본다."""
        attrs = self._sftp_call(f"폴더 읽기 ({path})", self.sftp.listdir_attr, path)
        entries: List[RemoteEntry] = []
        for a in attrs:
            mode = a.st_mode or 0
            is_dir, link = stat.S_ISDIR(mode), stat.S_ISLNK(mode)
            full = posixpath.join(path, a.filename)
            if link:
                try:
                    is_dir = stat.S_ISDIR(self.sftp.stat(full).st_mode or 0)
                except (IOError, OSError):
                    is_dir = False
            entries.append(RemoteEntry(a.filename, full, is_dir, int(a.st_size or 0), float(a.st_mtime or 0), link))
        entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
        return entries

    def upload_paths(self, paths: Sequence[Path], remote_dir: str, progress: Optional[Progress] = None) -> int:
        """파일·폴더들을 remote_dir 아래에 같은 이름으로 올린다. 반환: 올린 파일 수."""
        files: List[Tuple[Path, str]] = []
        for p in paths:
            if p.is_dir():
                files += [(f, posixpath.join(remote_dir, p.name, f.relative_to(p).as_posix()))
                          for f in sorted(p.rglob("*")) if f.is_file()]
            elif p.is_file():
                files.append((p, posixpath.join(remote_dir, p.name)))
            else:
                raise RemoteError(f"올릴 파일이 없습니다: {p}")
        total = sum(f.stat().st_size for f, _t in files) or 1
        sent = 0
        for f, target in files:
            self._sftp_call(f"올리기 ({f.name})", self._mkdirs, posixpath.dirname(target))
            self._sftp_call(f"올리기 ({f.name})", self.sftp.put, str(f), target)
            sent += f.stat().st_size
            if progress:
                progress(sent, total)
        return len(files)

    def download(self, entry: RemoteEntry, local_dir: Path, progress: Optional[Progress] = None) -> int:
        """파일(또는 폴더 통째로)을 local_dir 아래에 같은 이름으로 내려받는다. 반환: 받은 파일 수."""
        items: List[Tuple[str, Path]] = []
        stack = [(entry, local_dir / entry.name)]
        while stack:
            e, dest = stack.pop()
            if e.is_dir:
                stack += [(c, dest / c.name) for c in self.listdir(e.path)]
            else:
                items.append((e.path, dest))
        for i, (remote_path, dest) in enumerate(items, 1):
            dest.parent.mkdir(parents=True, exist_ok=True)
            self._sftp_call(f"내려받기 ({posixpath.basename(remote_path)})", self.sftp.get, remote_path, str(dest))
            if progress:
                progress(i, len(items))
        return len(items)

    def mkdir(self, path: str) -> None:
        self._sftp_call(f"새 폴더 ({path})", self.sftp.mkdir, path)

    def rename(self, path: str, new_path: str) -> None:
        self._sftp_call(f"이름 바꾸기 ({posixpath.basename(path)})", self.sftp.rename, path, new_path)

    def remove(self, entry: RemoteEntry) -> int:
        """파일 하나, 또는 폴더를 안의 것까지 지운다. 반환: 지운 파일·폴더 수."""
        if not entry.is_dir or entry.link:
            self._sftp_call(f"지우기 ({entry.name})", self.sftp.remove, entry.path)
            return 1
        count = 0
        for child in self.listdir(entry.path):
            count += self.remove(child)
        self._sftp_call(f"지우기 ({entry.name})", self.sftp.rmdir, entry.path)
        return count + 1

    def open_shell(self, on_data: Callable[[bytes], None], on_closed: Callable[[], None],
                   cols: int = 80, rows: int = 24, term: str = "xterm-256color", tag: str = "") -> Shell:
        """대화형 셸을 연다 (서버 화면의 터미널). 반영에 쓰는 명령 채널과는 별개 채널.
        tag 를 주면 환경 변수로 표식을 심어 shell_cwd() 가 그 셸을 찾을 수 있게 한다 (서버가 거부하면 조용히 무시됨)."""
        import paramiko
        try:
            chan = self.client.get_transport().open_session()
            if tag:
                for name, value in ((SHELL_TAG_VAR, tag), ("XMODIFIERS", f"@im=shipkit{tag}")):
                    try:
                        chan.set_environment_variable(name, value)
                    except paramiko.SSHException:
                        pass
            chan.get_pty(term=term, width=cols, height=rows)
            chan.invoke_shell()
        except (paramiko.SSHException, OSError, EOFError, AttributeError) as e:
            raise RemoteError(f"터미널을 열지 못했습니다: {e}") from e
        return Shell(chan, on_data, on_closed)

    def capture(self, command: str, timeout: float = 15.0) -> str:
        """명령 하나를 별도 채널에서 돌려 출력(stdout)을 돌려준다. 짧은 조회용 (진행 표시·비밀번호 없음)."""
        import paramiko
        try:
            chan = self.client.get_transport().open_session()
            chan.settimeout(timeout)
            chan.exec_command(command)
            chunks = []
            while True:
                data = chan.recv(65536)
                if not data:
                    break
                chunks.append(data)
            chan.close()
        except (paramiko.SSHException, OSError, EOFError, AttributeError) as e:
            raise RemoteError(f"서버에서 명령을 실행하지 못했습니다: {e}") from e
        return b"".join(chunks).decode("utf-8", "replace")

    def shell_cwd(self, tag: str, pid: int = 0) -> Tuple[int, str]:
        """표식 tag 로 연 셸의 (PID, 현재 폴더). pid 를 알면 건네 /proc 을 다시 훑지 않는다. 못 찾으면 (0, '')."""
        return parse_cwd_output(self.capture(CWD_SCRIPT % {"tag": shlex.quote(tag), "pid": int(pid)}))

    def close(self) -> None:
        if self._sftp is not None:
            self._sftp.close()
        self.client.close()


def _is_password_prompt(text: str) -> bool:
    """가상 터미널 출력의 끝이 암호 질문인지. 'Password:' 와 번역된 질문(콜론은 전각 '：' 일 수도) 을 알아본다."""
    tail = text.strip().lower()
    if not tail.endswith((":", "：")):
        return False
    last = tail.rsplit("\n", 1)[-1]   # 질문은 마지막 줄에 있다 (앞 줄의 평범한 출력에 든 단어로 오판하지 않게)
    return any(word in last for word in PASSWORD_WORDS)


def _sudo_refused(output: str) -> bool:
    """sudo -n 이 비밀번호가 없어 실행을 거부했는지. 영어 문구가 아니어도(번역된 로케일) 'sudo:' 한 줄만 찍고 끝나면 그렇게 본다."""
    if SUDO_NEEDS_PASSWORD in output:
        return True
    lines = [ln for ln in output.replace("\r", "").split("\n") if ln.strip()]
    return len(lines) == 1 and lines[0].startswith("sudo:")


# ---------------------------------------------------------------------- 명령 만들기
def expand_dir(path: str, home: str) -> str:
    if path == "~":
        return home
    if path.startswith("~/"):
        return posixpath.join(home, path[2:])
    return path


def remote_package_dir(profile: ServerProfile, package_name: str, home: str) -> str:
    """패키지를 올릴 서버 폴더 (SFTP 가 쓰는 / 구분자). Windows 서버의 'D:/deploy' 는 SFTP 형식 '/D:/deploy' 로."""
    base = expand_dir(profile.upload_dir, home)
    if profile.server_os == "windows" and re.match(r"^[A-Za-z]:", base):
        base = "/" + base
    return posixpath.join(base, package_name)


def to_windows_path(path: str) -> str:
    """SFTP 형식 경로(/C:/Users/x 또는 C:/Users/x) 를 PowerShell 에 줄 Windows 경로(C:\\Users\\x) 로."""
    if re.match(r"^/[A-Za-z]:", path):
        path = path[1:]
    return path.replace("/", "\\")


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def powershell_command(script: str) -> str:
    """Windows OpenSSH 서버에서 기본 셸(cmd 또는 PowerShell)에 상관없이 PowerShell 코드를 돌리는 명령.
    -EncodedCommand 라 따옴표·공백·한글이 셸을 거치지 않는다. 출력은 UTF-8 로 받는다."""
    body = "[Console]::OutputEncoding = [Text.Encoding]::UTF8; " + script
    encoded = base64.b64encode(body.encode("utf-16-le")).decode("ascii")
    return f"powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand {encoded}"


def decode_powershell_command(command: str) -> str:
    """powershell_command 가 만든 명령에서 PowerShell 코드를 되돌린다 (테스트·보기용)."""
    return base64.b64decode(command.rsplit(" ", 1)[-1]).decode("utf-16-le")


def _sudo(password: bool) -> str:
    """비밀번호를 보낼 때는 표준 입력으로 받고(-S), 아니면 묻지 않고 바로 실패하게 한다(-n)."""
    return "sudo -S -p ''" if password else "sudo -n"


def as_runner(profile: ServerProfile, inner: str, password: Optional[bool] = None) -> str:
    """inner(셸 명령)를 실행 계정으로 실행하는 명령. password: sudo 비밀번호를 보내는지 (None = 설정의 run_password)."""
    if not profile.run_as:
        return f"bash -c {shlex.quote(inner)}"
    if profile.run_with == "su":
        # LC_ALL=C: 암호 질문이 번역되지 않고 "Password:" 로 나오게 (su - 는 환경을 비우므로 안쪽 명령에는 안 넘어간다)
        return f"LC_ALL=C su - {shlex.quote(profile.run_as)} -c {shlex.quote(inner)}"
    if password is None:
        password = profile.run_password
    return f"{_sudo(password)} -u {shlex.quote(profile.run_as)} bash -c {shlex.quote(inner)}"


def script_command(profile: ServerProfile, package_dir: str, action: str, yes: bool = False,
                   no_hooks: bool = False, script: Optional[str] = None, password: Optional[bool] = None) -> str:
    """패키지 폴더에서 서버 스크립트를 실행하는 명령 (리눅스: 실행 계정으로 bash, Windows: PowerShell)."""
    if profile.server_os == "windows":
        ps_script = to_windows_path(posixpath.join(package_dir, script or SCRIPT_NAME_PS))
        args = [_ps_quote(action)]
        if profile.server_path:
            args.append(_ps_quote(profile.server_path))
        if yes:
            args.append("-Yes")
        if no_hooks:
            args.append("-NoHooks")
        return powershell_command(f"& {_ps_quote(ps_script)} " + " ".join(args) + "; exit $LASTEXITCODE")
    args = [action]
    if profile.server_path:
        args.append(profile.server_path)
    if yes:
        args.append("-y")
    if no_hooks:
        args.append("--no-hooks")
    inner = f"cd {shlex.quote(package_dir)} && bash {script or SCRIPT_NAME} " + " ".join(shlex.quote(a) for a in args)
    return as_runner(profile, inner, password)


def stream_upload(profile: ServerProfile) -> bool:
    """패키지를 실행 계정으로 바로 올리는지: 다른 계정으로 실행 + sudo (리눅스).
    SFTP 는 접속 계정으로만 되므로, 패키지를 tar 로 묶어 'sudo -u 계정 tar' 에 흘려 넣는다. 그러면 올릴 위치는
    실행 계정만 쓸 수 있으면 되고 소유자 변경(chown)도 필요 없다. su 는 비밀번호를 가상 터미널로 넣어야 해서
    이진 스트림을 보낼 수 없으니 SFTP + chown 그대로."""
    return bool(profile.run_as) and profile.run_with == "sudo" and profile.server_os != "windows"


def upload_command(profile: ServerProfile, remote_dir: str, password: Optional[bool] = None) -> str:
    """실행 계정으로 패키지 폴더를 만들고 표준 입력의 tar.gz 를 푸는 명령."""
    q = shlex.quote(remote_dir)
    return as_runner(profile, f"mkdir -p {q} && tar xzf - -C {q}", password)


LOG_TAIL_LINES = 200   # 로그 따라가기: 마지막 몇 줄부터 보여 줄지


def tail_command(profile: ServerProfile) -> str:
    """서버 연결에 적어 둔 로그 파일을 따라가는 명령 (서버 화면의 터미널에 친다). 마지막 줄들부터 보여 주고 새 줄을 기다린다.
    리눅스는 tail -F (로그가 돌아가며 새 파일이 되어도 따라감), Windows 는 PowerShell 의 Get-Content -Wait."""
    if profile.server_os == "windows":
        return f"Get-Content -Path {_ps_quote(to_windows_path(profile.log_file))} -Tail {LOG_TAIL_LINES} -Wait"
    return f"tail -n {LOG_TAIL_LINES} -F {shlex.quote(profile.log_file)}"


def package_archive(local_dir: Path) -> Path:
    """패키지 폴더를 임시 tar.gz 로 묶는다 (실행 계정으로 바로 올릴 때 보내는 스트림). 다 쓴 뒤 지울 것.
    권한은 리눅스 기준으로 정한다: 파일 644, 서버 스크립트·hooks/*.sh 755 (Windows 의 권한 비트는 믿을 수 없다)."""
    fd, name = tempfile.mkstemp(prefix="shipkit-", suffix=".tar.gz")
    os.close(fd)
    path = Path(name)

    def fix(info: tarfile.TarInfo) -> tarfile.TarInfo:
        info.uid = info.gid = 0
        info.uname = info.gname = ""
        rel = info.name
        if info.isdir():
            info.mode = 0o755
        elif rel in EXECUTABLES or (rel.startswith("hooks/") and rel.endswith(".sh")):
            info.mode = 0o755
        else:
            info.mode = 0o644
        return info

    try:
        with tarfile.open(path, "w:gz", compresslevel=1) as tar:
            for f in sorted(local_dir.rglob("*")):
                if f.is_file():
                    tar.add(f, arcname=f.relative_to(local_dir).as_posix(), filter=fix)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return path


def runner_home(conn: Connection, profile: ServerProfile, opts: dict, output: Optional[Output] = None) -> Optional[str]:
    """실행 계정의 홈 폴더 (실행 계정으로 바로 올릴 때 올릴 위치의 ~ 는 실행 계정 기준).
    알아내지 못하면(sudo 거부 등) 서버 출력과 안내를 output 으로 내보내고 None. output 이 없으면 RemoteError."""
    lines: List[str] = []
    cmd = as_runner(profile, 'printf %s "$HOME"', opts["password"] is not None)
    code = _run_sudo_aware(conn, profile, cmd, lines.append, opts)
    home = ("".join(lines).strip().splitlines() or [""])[-1].strip()
    if code or not home:
        text = f"{profile.runner} 계정의 홈 폴더를 알아내지 못했습니다:\n{''.join(lines).strip()}\n"
        if output is None:
            raise RemoteError(text.strip())
        output(text)
        return None
    return home


def chown_command(profile: ServerProfile, package_dir: str, password: Optional[bool] = None) -> Optional[str]:
    """올린 패키지 폴더를 실행 계정 소유로 (접속 계정이 root 가 아니면 sudo).
    password: sudo 비밀번호를 보내는지 (None = 설정의 run_password)."""
    if not (profile.chown and profile.run_as) or profile.server_os == "windows":
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
    if not profile.run_as or profile.server_os == "windows":
        return {"password": None, "pty": False}
    if profile.run_with == "su":
        return {"password": (secrets.run or "") if profile.run_password else None, "pty": True}
    # sudo: 비밀번호가 있으면(설정에서 켰거나, 소유자 변경 때문에 이미 물어봤으면) 보낸다
    password = secrets.run if secrets.run else ("" if profile.run_password else None)
    return {"password": password, "pty": False}


# ---------------------------------------------------------------------- 연결 진단
REQUIRED_TOOLS = ("bash", "tar", "cmp", "stat")   # 서버 스크립트가 꼭 쓰는 명령
CHECKSUM_TOOLS = ("sha256sum", "shasum")          # 둘 중 하나는 있어야 패키지를 검증한다 (없으면 건너뛴다)


@dataclass
class Check:
    """연결 진단 항목 하나."""
    name: str
    state: str          # ok | warn | fail | info
    text: str
    fix: str = ""       # 고칠 방법 (설정이나 서버에서)


@dataclass
class Diagnosis:
    """[연결 시험] 결과: 항목별 확인 결과와, 서버 연결 설정에 넣으면 되는 추천값."""
    address: str
    checks: List[Check] = field(default_factory=list)
    suggestions: Dict[str, Any] = field(default_factory=dict)   # run_with / run_password / chown

    @property
    def ok(self) -> bool:
        return not any(c.state == "fail" for c in self.checks)

    def summary(self) -> str:
        """글로 (명령어·알림·테스트용): 첫 줄은 접속 결과, 그 뒤 항목마다 한 줄."""
        marks = {"ok": "✓", "warn": "△", "fail": "✗", "info": "·"}
        connected = bool(self.checks) and self.checks[0].state != "fail"
        lines = [f"접속 성공: {self.address}" if connected else f"접속 실패: {self.address}"]
        for c in self.checks:
            lines.append(f"{marks[c.state]} {c.name}: {c.text}")
            if c.fix:
                lines.append(f"    → {c.fix}")
        return "\n".join(lines)


def _capture(conn: Connection, cmd: str, **opts) -> Tuple[int, str]:
    """명령 하나의 (종료 코드, 출력). 암호 질문 등으로 중단되면(RemoteError) 그 설명을 출력으로."""
    lines: List[str] = []
    try:
        code = conn.run(cmd, lines.append, **opts)
    except RemoteError as e:
        return 1, str(e)
    return code, "".join(lines).strip()


def _last_line(text: str) -> str:
    return (text.strip().splitlines() or [""])[-1].strip()


def diagnose(conn: Connection, profile: ServerProfile, secrets: Secrets, server_path: str = "") -> Diagnosis:
    """[연결 시험]: 접속 계정, 실행 계정 전환, 올릴 위치, 배포 경로, 필요한 명령, 로그 파일을 서버에서 확인하고
    설정을 어떻게 고치면 되는지 추천한다 (리눅스). 비밀번호는 설정대로만 쓰고, 다른 전환 방법은 비밀번호 없는 sudo -n 으로만
    더듬어 본다 (su 는 비밀번호 없이는 알 수 없다)."""
    d = Diagnosis(address=profile.address)
    q = shlex.quote
    code, out = _capture(conn, "id -un")
    who = _last_line(out)
    if code or not who:
        d.checks.append(Check("접속", "fail", f"접속은 됐지만 명령을 실행하지 못했습니다: {out}"))
        return d
    code2, uname = _capture(conn, "uname -sr")
    d.checks.append(Check("접속", "ok", f"{who} 계정으로 접속" + (f"  ({_last_line(uname)})" if not code2 and uname else "")))

    # 실행 계정: 설정대로 실행해 보고, 안 되거나 더 나은 방법이 있으면 추천한다
    opts = run_password(profile, secrets)
    with_pw = opts["password"] is not None
    runner_ok = True
    if profile.run_as:
        code, out = _capture(conn, as_runner(profile, "id -un", with_pw), **opts)
        runner_ok = code == 0 and _last_line(out) == profile.runner
        nopw_sudo = sudo_refused = False
        if who != profile.run_as:
            probe = replace(profile, run_with="sudo", run_password=False)
            c, o = _capture(conn, as_runner(probe, "id -un", False))
            nopw_sudo = c == 0 and _last_line(o) == profile.run_as
            sudo_refused = not nopw_sudo and _sudo_refused(o)
        how = "sudo -u" if profile.run_with == "sudo" else "su -"
        if runner_ok:
            text = f"{profile.runner} 계정으로 실행됩니다 ({how}, {'비밀번호 사용' if with_pw else '비밀번호 없이'})"
            if profile.run_with == "sudo" and with_pw and nopw_sudo:
                d.checks.append(Check("실행 계정", "warn", text, "비밀번호 없이도 sudo 가 되므로 '실행할 때 비밀번호가 필요합니다' 를 꺼도 됩니다."))
                d.suggestions["run_password"] = False
            elif profile.run_with == "su" and nopw_sudo:
                d.checks.append(Check("실행 계정", "warn", text,
                                      f"비밀번호 없이 sudo -u {profile.run_as} 가 됩니다. 전환 방법을 'sudo -u 계정' 으로 바꾸면 비밀번호 없이 "
                                      "실행하고 패키지도 실행 계정으로 바로 올립니다 (올릴 위치는 실행 계정만 쓸 수 있으면 되고 소유자 변경이 필요 없음)."))
                d.suggestions.update(run_with="sudo", run_password=False, chown=False)
            else:
                d.checks.append(Check("실행 계정", "ok", text))
        else:
            if profile.run_with == "sudo":
                if nopw_sudo:
                    fix = "비밀번호 없이 sudo 가 됩니다. '실행할 때 비밀번호가 필요합니다' 를 끄세요 (보낸 비밀번호가 맞지 않았습니다)."
                    d.suggestions["run_password"] = False
                elif sudo_refused and not with_pw:
                    fix = (f"{who} 계정의 sudo 에 비밀번호가 필요합니다. '실행할 때 비밀번호가 필요합니다' 를 켜거나, "
                           f"서버의 sudoers 에 '{who} ALL=({profile.run_as}) NOPASSWD: ALL' 을 넣으세요.")
                    d.suggestions["run_password"] = True
                elif sudo_refused:
                    fix = "sudo 비밀번호가 맞지 않거나 거부됐습니다. [저장된 비밀번호 지우기] 뒤 다시 시험하세요."
                else:
                    fix = (f"{who} 계정이 sudo -u {profile.run_as} 를 할 수 없습니다 (sudoers). 서버 관리자에게 "
                           f"'{who} ALL=({profile.run_as}) NOPASSWD: ALL' 추가를 요청하거나, 실행 계정의 비밀번호를 안다면 "
                           "전환 방법을 'su - 계정' 으로 바꾸고 '실행할 때 비밀번호가 필요합니다' 를 켜세요.")
            elif nopw_sudo:
                fix = (f"비밀번호 없이 sudo -u {profile.run_as} 가 됩니다. 전환 방법을 'sudo -u 계정' 으로 바꾸세요 "
                       "(비밀번호 없이 실행하고 패키지도 실행 계정으로 바로 올립니다).")
                d.suggestions.update(run_with="sudo", run_password=False, chown=False)
            elif not with_pw:
                fix = "su 는 실행 계정의 비밀번호가 필요합니다. '실행할 때 비밀번호가 필요합니다' 를 켜세요."
                if sudo_refused:
                    fix += f" (또는 {who} 계정의 sudo 비밀번호를 안다면 전환 방법 'sudo -u 계정' + 비밀번호 필요)"
                d.suggestions["run_password"] = True
            else:
                fix = "실행 계정의 비밀번호가 맞지 않습니다. [저장된 비밀번호 지우기] 뒤 다시 시험하세요."
            d.checks.append(Check("실행 계정", "fail", f"{profile.runner} 계정으로 실행하지 못했습니다 ({how}): {_last_line(out)}", fix))
    else:
        d.checks.append(Check("실행 계정", "ok", f"접속 계정 {who} 그대로 실행"))

    def as_runner_ok(inner: str) -> bool:
        c, _o = _capture(conn, as_runner(profile, inner, with_pw), **opts)
        return c == 0

    # 올릴 위치: 실행 계정으로 바로 올리면(sudo) 실행 계정이, 아니면 접속 계정이 쓸 수 있어야 한다
    streams = stream_upload(profile)
    if streams and not runner_ok:
        d.checks.append(Check("올릴 위치", "info", f"{profile.upload_dir}  (실행 계정 전환이 안 돼 확인하지 못했습니다)"))
    else:
        try:
            home = runner_home(conn, profile, opts) if streams and profile.upload_dir.startswith("~") else conn.home()
        except RemoteError as e:
            home = ""
            d.checks.append(Check("올릴 위치", "fail", str(e)))
        if home:
            upload = expand_dir(profile.upload_dir, home)
            probe = f"mkdir -p {q(upload)} && test -w {q(upload)}"
            writable = as_runner_ok(probe) if streams else _capture(conn, probe)[0] == 0
            account = profile.runner if streams else who
            if writable:
                d.checks.append(Check("올릴 위치", "ok", f"{upload}  ({account} 계정이 씀)"))
            elif not streams and profile.run_as and runner_ok and as_runner_ok(probe):
                fix = (f"접속 계정 {who} 는 쓸 수 없지만 실행 계정 {profile.runner} 는 쓸 수 있습니다. 전환 방법이 'sudo -u 계정' 이면 "
                       "실행 계정으로 바로 올리므로 그대로 됩니다. su 를 써야 하면 두 계정이 모두 쓸 수 있는 폴더로 바꾸세요.")
                d.checks.append(Check("올릴 위치", "fail", f"{upload} 에 {account} 계정이 쓸 수 없습니다.", fix))
            else:
                d.checks.append(Check("올릴 위치", "fail", f"{upload} 에 {account} 계정이 쓸 수 없습니다.",
                                      f"{account} 계정이 쓸 수 있는 폴더로 바꾸거나 서버에서 권한을 주세요."))

    # 배포 경로: 실행 계정이 들어가고 쓸 수 있어야 apply 가 된다
    target = profile.server_path or server_path
    if target:
        if not runner_ok:
            d.checks.append(Check("배포 경로", "info", f"{target}  (실행 계정 전환이 안 돼 확인하지 못했습니다)"))
        elif not as_runner_ok(f"test -d {q(target)}"):
            d.checks.append(Check("배포 경로", "fail", f"{target} 폴더가 없습니다 ({profile.runner} 계정 기준).",
                                  "서버 배포 경로를 확인하세요. Java 웹이면 WEB-INF 바로 위 폴더입니다."))
        elif not as_runner_ok(f"test -w {q(target)}"):
            d.checks.append(Check("배포 경로", "warn", f"{target} 에 {profile.runner} 계정이 쓸 수 없습니다.",
                                  "기존 파일만 바꾸는 반영은 되지만 새 파일·폴더가 생기면 실패합니다. 실행 계정에 쓰기 권한을 주세요."))
        else:
            d.checks.append(Check("배포 경로", "ok", f"{target}  ({profile.runner} 계정이 씀)"))

    # 서버 스크립트가 쓰는 명령
    tools = " ".join(REQUIRED_TOOLS + CHECKSUM_TOOLS)
    _c, out = _capture(conn, f"for c in {tools}; do command -v \"$c\" >/dev/null 2>&1 || printf '%s ' \"$c\"; done")
    missing = [m for m in out.split() if m in REQUIRED_TOOLS + CHECKSUM_TOOLS]
    required = [m for m in missing if m in REQUIRED_TOOLS]
    if required:
        d.checks.append(Check("필요한 명령", "fail", f"서버에 {', '.join(required)} 이(가) 없습니다.",
                              "서버 스크립트(shipkit.sh)가 쓰는 명령입니다. 서버에 설치하세요 (coreutils·diffutils·tar)."))
    elif all(t in missing for t in CHECKSUM_TOOLS):
        d.checks.append(Check("필요한 명령", "warn", "sha256sum·shasum 이 없어 올린 패키지의 손상 검증을 건너뜁니다."))
    else:
        d.checks.append(Check("필요한 명령", "ok", "bash · tar · cmp · stat · sha256sum 모두 있습니다."))

    # 로그 파일: [로그 보기]는 접속 계정의 터미널에서 tail 한다
    if profile.log_file:
        if _capture(conn, f"test -r {q(profile.log_file)}")[0] == 0:
            d.checks.append(Check("로그 파일", "ok", f"{profile.log_file}  ({who} 계정이 읽음)"))
        elif profile.run_as and runner_ok and as_runner_ok(f"test -r {q(profile.log_file)}"):
            d.checks.append(Check("로그 파일", "warn", f"{profile.log_file} 을 접속 계정 {who} 는 읽을 수 없고 실행 계정만 읽습니다.",
                                  "[로그 보기]는 접속 계정의 터미널에서 tail 하므로 파일에 읽기 권한을 주거나 터미널에서 sudo 를 붙이세요."))
        else:
            d.checks.append(Check("로그 파일", "warn", f"{profile.log_file} 을 읽을 수 없습니다 (없거나 권한 없음).",
                                  "서버 연결의 로그 파일 경로를 확인하세요."))
    return d


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
    """패키지를 올리고(필요하면 소유자 변경) check 를 실행한다. 다른 계정 + sudo 면 실행 계정으로 바로 올린다."""
    if stream_upload(profile):
        opts = run_password(profile, secrets)
        home = ""
        if profile.upload_dir.startswith("~"):
            home = runner_home(conn, profile, opts, output)
            if home is None:
                return StepResult("upload", 1, "")
        remote_dir = remote_package_dir(profile, package_dir.name, home)
        cmd = upload_command(profile, remote_dir, opts["password"] is not None)
        output(f"$ 올리기: {package_dir.name} → {profile.address}:{remote_dir}  ({profile.runner} 계정으로 바로 풀기)\n$ {cmd}\n")
        seen: List[str] = []

        def out(text: str) -> None:
            seen.append(text)
            output(text)

        code = conn.upload_stream(cmd, package_dir, out, progress, opts["password"])
        if code:
            if opts["password"] is None and cmd.startswith("sudo -n") and _sudo_refused("".join(seen)):
                output(f"[안내] {profile.user} 계정의 sudo 에 비밀번호가 필요합니다. "
                       "서버 연결 설정에서 '비밀번호 필요(run_password)'를 켜거나, 서버의 sudoers 에 NOPASSWD 를 설정하세요.\n")
            return StepResult("upload", code, remote_dir)
        return run_action(conn, profile, secrets, remote_dir, "check", output)
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
    if code and opts["password"] is None and cmd.startswith("sudo -n") and _sudo_refused("".join(seen)):
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
    if profile.server_os == "windows":  # 서버 스크립트(shipkit.ps1)와 같은 규칙: '~/' 는 $HOME 아래
        if history_file.startswith("~/"):
            expr = f"(Join-Path $HOME {_ps_quote(history_file[2:].replace('/', chr(92)))})"
        else:
            expr = _ps_quote(to_windows_path(history_file))
        command = powershell_command(f"$p = {expr}; if (Test-Path -LiteralPath $p) "
                                     "{ Get-Content -LiteralPath $p -Encoding UTF8 }; exit 0")
    else:
        if history_file.startswith("~/"):  # 서버 스크립트와 같은 규칙으로 실행 계정의 집 폴더
            path = '"$HOME"/' + shlex.quote(history_file[2:])
        else:
            path = shlex.quote(history_file)
        command = as_runner(profile, f"cat {path} 2>/dev/null || true", opts["password"] is not None)
    lines: List[str] = []
    code = _run_sudo_aware(conn, profile, command, lines.append, opts)
    if code:
        output("".join(lines))
        raise RemoteError(f"서버 이력을 읽지 못했습니다 (종료 코드 {code}).")
    return parse_history("".join(lines))
