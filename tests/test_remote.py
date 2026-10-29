"""서버에 바로 올리고 실행하기: 테스트용 SSH 서버에 실제 SSHConnection 으로 접속해서 확인한다."""
from datetime import datetime
from pathlib import Path

import pytest

pytest.importorskip("paramiko")

from shipkit import mapper, packager, remote  # noqa: E402
from shipkit.config import ConfigError, ServerProfile, parse_config  # noqa: E402
from shipkit.models import ChangeType as C, FileChange, SourceInfo  # noqa: E402

from conftest import make_config, write  # noqa: E402
from sshd import LOGIN_PW, SU_PW, SUDO_PW, FakeSSHServer  # noqa: E402
from test_package_e2e import BASH, needs_bash  # noqa: E402


def bash_path(p: Path) -> str:
    """Git Bash 가 아는 리눅스식 절대 경로 (C:/x -> /c/x): 설정의 server_path 검사를 통과하게."""
    posix = p.as_posix()
    return f"/{posix[0].lower()}{posix[2:]}" if posix[1:2] == ":" else posix


@pytest.fixture
def sshd(tmp_path):
    if BASH is None:
        pytest.skip("bash 가 없습니다")
    home = tmp_path / "home"
    home.mkdir()
    server = FakeSSHServer(home, BASH)
    yield server
    server.close()


def _profile(sshd, **over) -> ServerProfile:
    values = dict(name="개발", host="127.0.0.1", port=sshd.port, user="deployer", upload_dir="~/deploy")
    values.update(over)
    return ServerProfile(**values)


def _package(maven_project, tmp_path, server_dir: Path):
    project = make_config(maven_project, tmp_path / "out", server_path=bash_path(server_dir)).project("erp")
    changes = [FileChange("src/main/webapp/WEB-INF/views/user/list.jsp", C.MODIFIED, "!1")]
    return packager.build_package(project, mapper.plan(project, changes), SourceInfo("manual", "t"),
                                  source_changes=changes, now=datetime(2026, 10, 30, 9, 0))


# ---------------------------------------------------------------------- 설정
def test_servers_config():
    base = {"root": "x", "output_dir": "y", "server_path": "/srv/app"}
    p = parse_config({"projects": {"a": dict(base, servers=[
        {"name": "개발", "host": "10.0.0.5", "user": "admin", "run_as": "app", "run_with": "su", "chown": True},
        {"name": "운영", "host": "prod.acme.local", "port": 2222, "user": "app", "auth": "key",
         "key_file": "C:/keys/id_ed25519", "server_path": "/srv/prod"}])}}).project("a")
    dev, prod = p.servers
    assert (dev.runner, dev.run_with, dev.chown, dev.upload_dir) == ("app", "su", True, "~/deploy")
    assert prod.address == "app@prod.acme.local:2222" and prod.server_path == "/srv/prod" and prod.runner == "app"
    for bad, msg in [({"name": "x", "host": "h"}, "user"), ({"name": "x", "host": "h", "user": "u", "auth": "key"}, "key_file"),
                     ({"name": "x", "host": "h; rm", "user": "u"}, "host"),
                     ({"name": "x", "host": "h", "user": "u", "upload_dir": "deploy"}, "upload_dir")]:
        with pytest.raises(ConfigError, match=msg):
            parse_config({"projects": {"a": dict(base, servers=[bad])}})


def test_commands_for_each_account_mode():
    plain = ServerProfile("a", "h", "app")
    assert remote.script_command(plain, "/home/app/deploy/p", "check") == \
        "bash -c 'cd /home/app/deploy/p && bash shipkit.sh check'"
    sudo = ServerProfile("a", "h", "admin", run_as="app", run_password=True, chown=True)
    assert remote.script_command(sudo, "/d/p", "apply", yes=True).startswith("sudo -S -p '' -u app bash -c ")
    assert remote.chown_command(sudo, "/d/p") == "sudo -S -p '' chown -R app: /d/p"
    su = ServerProfile("a", "h", "root", run_as="app", run_with="su", server_path="/srv/other")
    assert remote.script_command(su, "/d/p", "rollback", no_hooks=True) == \
        "su - app -c 'cd /d/p && bash shipkit.sh rollback /srv/other --no-hooks'"
    assert remote.chown_command(su, "/d/p") is None                      # chown 안 켬
    assert remote.expand_dir("~/deploy", "/home/app") == "/home/app/deploy"
    # 비밀번호를 보내지 않을 때는 sudo 가 묻지 않고 바로 실패하게(-n) 해서 영원히 기다리는 일이 없게 한다
    nopw = ServerProfile("a", "h", "admin", run_as="app", chown=True)
    assert remote.script_command(nopw, "/d/p", "check").startswith("sudo -n -u app bash -c ")
    assert remote.chown_command(nopw, "/d/p") == "sudo -n chown -R app: /d/p"
    assert remote.chown_command(nopw, "/d/p", password=True) == "sudo -S -p '' chown -R app: /d/p"
    # 소유자 변경 때문에 sudo 비밀번호를 이미 받았으면 스크립트 실행에도 그 비밀번호를 쓴다
    assert remote.run_password(nopw, remote.Secrets(run="pw")) == {"password": "pw", "pty": False}
    assert remote.run_password(nopw, remote.Secrets()) == {"password": None, "pty": False}
    assert remote.run_password(su, remote.Secrets(run="pw")) == {"password": None, "pty": True}  # su 는 설정대로만


@needs_bash
def test_missing_run_password_fails_fast(sshd, tmp_path, maven_project):
    """비밀번호가 필요한데 설정에서 끄면: 기다리지 않고 바로 실패하고, 고칠 방법을 알려 준다."""
    server = tmp_path / "server"
    server.mkdir()
    result = _package(maven_project, tmp_path, server)
    out = []
    sudo = _profile(sshd, user="admin", run_as="app")
    conn = _connect(sshd, tmp_path, sudo, remote.Secrets(login=LOGIN_PW))
    try:
        res = remote.upload_and_check(conn, sudo, remote.Secrets(LOGIN_PW), result.out_dir, out.append)
        text = "".join(out)
        assert not res.ok and "a password is required" in text and "run_password" in text
        sshd.sudo_nopasswd = True                                        # NOPASSWD 서버면 비밀번호 없이 된다
        res = remote.run_action(conn, sudo, remote.Secrets(LOGIN_PW), res.remote_dir, "check", out.append)
        assert res.ok and sshd.ran_as[-1] == "sudo:app", "".join(out)
    finally:
        conn.close()

    su = _profile(sshd, user="admin", run_as="app", run_with="su")
    conn = _connect(sshd, tmp_path, su, remote.Secrets(login=LOGIN_PW))
    try:
        with pytest.raises(remote.RemoteError, match="비밀번호가 필요"):
            remote.run_action(conn, su, remote.Secrets(LOGIN_PW), res.remote_dir, "check", out.append)
    finally:
        conn.close()


# ---------------------------------------------------------------------- 실제 접속 (테스트용 서버)
def test_host_key_new_known_changed(sshd, tmp_path):
    known = tmp_path / "known_hosts"
    p = _profile(sshd)
    status = remote.check_host_key(p, known)
    assert status.state == "new" and status.fingerprint.startswith("SHA256:")
    remote.remember_host_key(p, status, known)
    assert remote.check_host_key(p, known).state == "known"
    sshd.key = __import__("paramiko").RSAKey.generate(1024)             # 서버를 다시 설치한 경우
    assert remote.check_host_key(p, known).state == "changed"


def _connect(sshd, tmp_path, profile, secrets):
    known = tmp_path / "known_hosts"
    remote.remember_host_key(profile, remote.check_host_key(profile, known), known)
    return remote.SSHConnection(profile, secrets, known)


@needs_bash
def test_upload_check_apply_rollback(sshd, tmp_path, maven_project):
    server = tmp_path / "server"
    write(server / "WEB-INF/views/user/list.jsp", "OLD")
    result = _package(maven_project, tmp_path, server)
    p = _profile(sshd)
    conn = _connect(sshd, tmp_path, p, remote.Secrets(login=LOGIN_PW))
    out = []
    try:
        res = remote.upload_and_check(conn, p, remote.Secrets(login=LOGIN_PW), result.out_dir, out.append)
        assert res.ok, "".join(out)
        assert res.remote_dir == (sshd.home / "deploy" / result.out_dir.name).as_posix()
        assert (sshd.home / "deploy" / result.out_dir.name / "files/WEB-INF/views/user/list.jsp").is_file()
        res = remote.backup_and_apply(conn, p, remote.Secrets(), res.remote_dir, out.append, run_hooks=False)
        assert res.ok and res.action == "apply", "".join(out)
        assert (server / "WEB-INF/views/user/list.jsp").read_text(encoding="utf-8") == "<%-- jsp --%>"
        assert remote.run_action(conn, p, remote.Secrets(), res.remote_dir, "rollback", out.append, no_hooks=True).ok
        assert (server / "WEB-INF/views/user/list.jsp").read_text(encoding="utf-8") == "OLD"
    finally:
        conn.close()


@needs_bash
def test_run_as_other_account_with_passwords(sshd, tmp_path, maven_project):
    server = tmp_path / "server"
    server.mkdir()
    result = _package(maven_project, tmp_path, server)
    su = _profile(sshd, user="admin", run_as="app", run_with="su", run_password=True)
    conn = _connect(sshd, tmp_path, su, remote.Secrets(login=LOGIN_PW))
    out = []
    try:
        res = remote.upload_and_check(conn, su, remote.Secrets(LOGIN_PW, SU_PW), result.out_dir, out.append)
        assert res.ok and sshd.ran_as == ["su:app"], "".join(out)
        assert "Password" not in "".join(out)                           # 암호 질문은 화면에 보이지 않게 답함
        bad = remote.run_action(conn, su, remote.Secrets(LOGIN_PW, "wrong"), res.remote_dir, "check", out.append)
        assert not bad.ok
    finally:
        conn.close()

    sudo = _profile(sshd, user="admin", run_as="app", run_password=True, chown=True)
    conn = _connect(sshd, tmp_path, sudo, remote.Secrets(login=LOGIN_PW))
    try:
        res = remote.upload_and_check(conn, sudo, remote.Secrets(LOGIN_PW, SUDO_PW), result.out_dir, out.append)
        assert res.ok, "".join(out)
        assert sshd.ran_as[-2:] == ["sudo:root", "sudo:app"]              # chown(관리자 권한) 다음 실행 계정으로 check
    finally:
        conn.close()


def test_login_failure(sshd, tmp_path):
    p = _profile(sshd)
    known = tmp_path / "known_hosts"
    remote.remember_host_key(p, remote.check_host_key(p, known), known)
    with pytest.raises(remote.RemoteAuthError, match="로그인에 실패"):
        remote.SSHConnection(p, remote.Secrets(login="nope"), known)
    unknown = tmp_path / "empty_known_hosts"                            # 지문을 확인하지 않은 서버에는 접속하지 않음
    with pytest.raises(remote.RemoteError):
        remote.SSHConnection(p, remote.Secrets(login=LOGIN_PW), unknown)
