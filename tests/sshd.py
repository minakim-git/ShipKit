"""테스트용 SSH/SFTP 서버 (paramiko). 실제 SSHConnection 코드로 올리기·실행·계정 전환을 시험한다.

- 로그인: 계정 아무거나, 비밀번호는 LOGIN_PW
- SFTP: 로컬 경로를 그대로 쓴다 (홈 = home 폴더)
- 명령: bash 로 실행. sudo -S / su - 는 흉내 낸다 (비밀번호 확인 후 실행 계정을 기록하고 안쪽 명령을 실행)
"""
from __future__ import annotations

import os
import shlex
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import List, Optional

import paramiko
from paramiko import SFTPAttributes, SFTPHandle, SFTPServer, SFTPServerInterface
from paramiko.sftp import SFTP_FAILURE, SFTP_NO_SUCH_FILE, SFTP_OK

LOGIN_PW = "login-pw"
SUDO_PW = "sudo-pw"
SU_PW = "su-pw"


class _Handle(SFTPHandle):
    def stat(self):
        return SFTPAttributes.from_stat(os.fstat(self.readfile.fileno() if hasattr(self, "readfile")
                                                  else self.writefile.fileno()))

    def chattr(self, attr):
        return SFTP_OK


class _SFTP(SFTPServerInterface):
    home = "/"

    @staticmethod
    def _local(path: str) -> str:
        """Windows OpenSSH 의 SFTP 처럼 '/C:/Users/x' 형식도 받아 로컬 경로로 쓴다."""
        return path[1:] if len(path) > 2 and path[0] == "/" and path[2] == ":" else path

    def _err(self, e):
        return SFTPServer.convert_errno(e.errno)

    def canonicalize(self, path):
        return self.home if path in (".", "") else path

    def stat(self, path):
        path = self._local(path)
        try:
            return SFTPAttributes.from_stat(os.stat(path))
        except OSError as e:
            return self._err(e)

    lstat = stat

    def list_folder(self, path):
        path = self._local(path)
        try:
            return [SFTPAttributes.from_stat(os.stat(os.path.join(path, n)), n) for n in os.listdir(path)]
        except OSError as e:
            return self._err(e)

    def open(self, path, flags, attr):
        path = self._local(path)
        try:
            mode = "wb" if flags & (os.O_WRONLY | os.O_RDWR) else "rb"
            f = open(path, mode)
        except OSError as e:
            return self._err(e)
        h = _Handle(flags)
        if mode == "wb":
            h.writefile = f
        else:
            h.readfile = f
        return h

    def mkdir(self, path, attr):
        path = self._local(path)
        try:
            os.mkdir(path)
        except OSError as e:
            return self._err(e)
        return SFTP_OK

    def chattr(self, path, attr):
        path = self._local(path)
        return SFTP_OK if os.path.exists(path) else SFTP_NO_SUCH_FILE

    def remove(self, path):
        path = self._local(path)
        try:
            os.remove(path)
        except OSError:
            return SFTP_FAILURE
        return SFTP_OK

    def rmdir(self, path):
        try:
            os.rmdir(self._local(path))
        except OSError:
            return SFTP_FAILURE
        return SFTP_OK

    def rename(self, oldpath, newpath):
        try:
            os.rename(self._local(oldpath), self._local(newpath))
        except OSError:
            return SFTP_FAILURE
        return SFTP_OK


# 로케일별 su 암호 질문과 sudo -n 거부 문구 (실제 서버의 gettext 번역)
MESSAGES = {
    "C": {"su": "Password: ", "sudo": "sudo: a password is required"},
    "ko": {"su": "암호: ", "sudo": "sudo: 암호가 필요합니다"},
    "ja": {"su": "パスワード: ", "sudo": "sudo: パスワードが必要です"},
}


class FakeSSHServer:
    def __init__(self, home: Path, bash: str):
        self.home = home
        self.bash = bash
        self.key = paramiko.RSAKey.generate(1024)
        self.commands: List[str] = []   # 받은 명령
        self.ran_as: List[str] = []     # 계정 전환 기록 (sudo:계정 / su:계정)
        self.sudo_nopasswd = False      # True 면 sudo -n 이 비밀번호 없이 된다 (sudoers NOPASSWD)
        self.locale = "C"               # 서버 로케일: su·sudo 문구가 번역된다 (C / ko / ja)
        self.pty_sizes: List[tuple] = []   # 셸(pty) 크기 요청 기록 (열, 줄)
        self.honor_env = True           # False 면 명령 앞의 LC_ALL=C 를 무시한다 (번역된 질문을 그대로 내는 서버)
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self._stop = False
        threading.Thread(target=self._accept, daemon=True).start()

    def close(self) -> None:
        self._stop = True
        self.sock.close()

    def _accept(self) -> None:
        while not self._stop:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            t = paramiko.Transport(conn)
            t.add_server_key(self.key)
            _SFTP.home = self.home.as_posix()
            t.set_subsystem_handler("sftp", SFTPServer, _SFTP)
            try:
                t.start_server(server=_Server(self))
            except Exception:
                continue


class _Server(paramiko.ServerInterface):
    def __init__(self, owner: FakeSSHServer):
        self.owner = owner

    def check_auth_password(self, username, password):
        return paramiko.AUTH_SUCCESSFUL if password == LOGIN_PW else paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):
        return "password"

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_pty_request(self, channel, term, width, height, *_args):
        self.owner.pty_sizes.append((width, height))
        return True

    def check_channel_window_change_request(self, channel, width, height, *_args):
        self.owner.pty_sizes.append((width, height))
        return True

    def check_channel_shell_request(self, channel):
        """대화형 셸 흉내: 친 글자를 되돌려 보내고(에코), Enter 면 bash 로 실행해 출력을 보낸 뒤 프롬프트."""
        threading.Thread(target=self._shell, args=(channel,), daemon=True).start()
        return True

    def _shell(self, channel) -> None:
        try:
            self._shell_loop(channel)
        except (OSError, EOFError):   # 손님이 먼저 끊었을 때 (Socket is closed)
            pass

    def _shell_loop(self, channel) -> None:
        prompt = b"fake$ "
        channel.sendall(prompt)
        line = b""
        while True:
            data = channel.recv(1024)
            if not data:
                return
            for b in data:
                if b in (13, 10):
                    command = line.decode("utf-8", "replace").strip()
                    line = b""
                    channel.sendall(b"\r\n")
                    if command == "exit":
                        channel.sendall(b"logout\r\n")
                        channel.close()
                        return
                    if command:
                        self.owner.commands.append(command)
                        result = subprocess.run([self.owner.bash, "-c", command], capture_output=True,
                                                cwd=str(self.owner.home))
                        channel.sendall((result.stdout + result.stderr).replace(b"\n", b"\r\n"))
                    channel.sendall(prompt)
                elif b == 127:
                    if line:
                        line = line[:-1]
                        channel.sendall(b"\b \b")
                elif b == 3:
                    line = b""
                    channel.sendall(b"^C\r\n" + prompt)
                else:
                    line += bytes([b])
                    channel.sendall(bytes([b]))

    def check_channel_exec_request(self, channel, command):
        command = command.decode() if isinstance(command, bytes) else command
        self.owner.commands.append(command)
        threading.Thread(target=self._run, args=(channel, command), daemon=True).start()
        return True

    def _readline(self, channel) -> str:
        data = b""
        while not data.endswith(b"\n"):
            chunk = channel.recv(1)
            if not chunk:
                break
            data += chunk
        return data.decode().rstrip("\r\n")

    def _run(self, channel, command: str) -> None:
        time.sleep(0.05)  # exec 요청에 대한 응답이 먼저 가도록 (바로 닫으면 손님 쪽이 'Channel closed')
        argv = shlex.split(command)
        env = {}
        while argv and "=" in argv[0] and not argv[0].startswith("-"):   # 앞의 VAR=value
            name, _, value = argv.pop(0).partition("=")
            env[name] = value
        locale = env.get("LC_ALL", self.owner.locale) if self.owner.honor_env else self.owner.locale
        msg = MESSAGES.get(locale, MESSAGES["C"])
        user: Optional[str] = None
        if argv[0] == "sudo":
            rest = argv[1:]
            flags = []
            while rest and rest[0].startswith("-"):
                flags.append(rest[0])
                if rest[0] in ("-p", "-u"):
                    if rest[0] == "-u":
                        user = rest[1]
                    rest = rest[2:]
                else:
                    rest = rest[1:]
            if "-n" in flags:  # 묻지 않고 실행: 비밀번호 없이 되는 서버(NOPASSWD)가 아니면 바로 실패
                if not self.owner.sudo_nopasswd:
                    channel.sendall(msg["sudo"].encode("utf-8") + b"\n")
                    channel.send_exit_status(1)
                    channel.close()
                    return
            elif self._readline(channel) != SUDO_PW:
                channel.sendall(b"sudo: incorrect password\n")
                channel.send_exit_status(1)
                channel.close()
                return
            argv = rest
            self.owner.ran_as.append(f"sudo:{user or 'root'}")
        elif argv[0] == "su":
            user = argv[2]
            channel.sendall(msg["su"].encode("utf-8"))
            if self._readline(channel) != SU_PW:
                try:
                    channel.sendall(b"su: Authentication failure\n")
                    channel.send_exit_status(1)
                except (OSError, EOFError):
                    pass  # 손님이 암호 질문을 보고 먼저 끊은 경우
                channel.close()
                return
            self.owner.ran_as.append(f"su:{user}")
            argv = ["bash", "-c", argv[4]]
        if argv[0] == "chown":
            channel.send_exit_status(0)
            channel.close()
            return
        if argv[:3] == ["bash", "-c", "id -un"]:
            channel.sendall(((user or "deployer") + "\n").encode())
            channel.send_exit_status(0)
            channel.close()
            return
        argv = [self.owner.bash] + argv[1:] if argv[0] == "bash" else [self.owner.bash, "-c", command]
        proc = subprocess.run(argv, capture_output=True, cwd=str(self.owner.home), stdin=subprocess.DEVNULL)
        try:
            channel.sendall(proc.stdout + proc.stderr)
            channel.send_exit_status(proc.returncode)
        except (OSError, EOFError):
            pass  # 손님이 [취소]로 먼저 끊은 경우
        channel.close()
