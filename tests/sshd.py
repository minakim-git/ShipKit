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


class FakeSSHServer:
    def __init__(self, home: Path, bash: str):
        self.home = home
        self.bash = bash
        self.key = paramiko.RSAKey.generate(1024)
        self.commands: List[str] = []   # 받은 명령
        self.ran_as: List[str] = []     # 계정 전환 기록 (sudo:계정 / su:계정)
        self.sudo_nopasswd = False      # True 면 sudo -n 이 비밀번호 없이 된다 (sudoers NOPASSWD)
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

    def check_channel_pty_request(self, *args):
        return True

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
                    channel.sendall(b"sudo: a password is required\n")
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
            channel.sendall(b"Password: ")
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
