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


@needs_bash
def test_cancel_and_idle_timeout(sshd, tmp_path):
    """[취소]는 멈춰 있는 명령을 바로 끊고, 서버 출력이 오래 없으면 저절로 중단한다 (영원히 기다리지 않는다)."""
    import threading
    import time
    p = _profile(sshd)
    conn = _connect(sshd, tmp_path, p, remote.Secrets(login=LOGIN_PW))
    out, errors = [], []

    def work():
        try:
            conn.run("bash -c 'echo started; sleep 30'", out.append)
        except remote.RemoteError as e:
            errors.append(e)

    t = threading.Thread(target=work, daemon=True)
    t.start()
    started = time.time()
    while "started" not in "".join(out) and time.time() - started < 10:
        time.sleep(0.05)
    conn.cancel()
    t.join(timeout=10)
    assert not t.is_alive() and len(errors) == 1 and isinstance(errors[0], remote.RemoteCancelled)
    with pytest.raises(remote.RemoteCancelled):                     # 끊은 연결은 더 쓰지 않는다
        conn.run("echo again", out.append)
    conn.close()

    quick = _connect(sshd, tmp_path, p, remote.Secrets(login=LOGIN_PW))
    quick.idle_timeout = 1
    try:
        assert quick.run("bash -c 'echo a; sleep 0.3; echo b'", out.append) == 0   # 출력이 이어지면 괜찮다
        with pytest.raises(remote.RemoteError, match="1초 동안 아무 출력이 없어"):
            quick.run("bash -c 'sleep 5'", out.append)
    finally:
        quick.close()


@needs_bash
def test_pty_output_is_not_lost(sshd, tmp_path):
    """가상 터미널(su)로 실행할 때 암호 질문을 지켜보느라 모아 둔 앞부분 출력이 사라지면 안 된다 (이력 조회가 들쭉날쭉하던 문제)."""
    p = _profile(sshd)
    conn = _connect(sshd, tmp_path, p, remote.Secrets(login=LOGIN_PW))
    try:
        for n in (3, 30, 300):   # 암호 감시 창(PROMPT_WINDOW)보다 짧은 출력, 긴 출력 모두
            out = []
            assert conn.run(f"bash -c 'for i in $(seq 1 {n}); do echo line-$i; done'", out.append, pty=True) == 0
            lines = [ln for ln in "".join(out).replace("\r\n", "\n").split("\n") if ln]
            assert lines == [f"line-{i}" for i in range(1, n + 1)], (n, lines[:3], len(lines))
    finally:
        conn.close()


def test_parse_check_output():
    text = ("$ check  (deployer 계정)\n[정보] 대상 경로: /srv/app\n== 반영 대상 (manifest.txt) ==\n"
            "  [신규] WEB-INF/classes/com/acme/New.class\n  [동일] css/a.css\n  [변경] WEB-INF/views/user/list.jsp\r\n"
            "  (서버와 같은 파일 1개는 apply 때 건드리지 않습니다)\n== 삭제 대상 (delete.txt) ==\n"
            "  [삭제예정] old.jsp\n  [이미없음] gone.jsp\n== 정리 대상 ==\n  [정리예정] assets/app.old.js\n"
            "== post_apply 명령 ==\n    sudo systemctl restart tomcat\n"
            "[정보] 신규 1 / 변경 1 / 동일 1 / 삭제예정 1 / 이미없음 1 / 정리예정 1\n")
    r = remote.parse_check(text)
    assert r.counts == {"신규": 1, "변경": 1, "동일": 1, "삭제예정": 1, "이미없음": 1, "정리예정": 1}
    assert r.paths("변경") == ["WEB-INF/views/user/list.jsp"] and r.changes_server == 4
    assert remote.parse_check("아무 관계 없는 출력\n") is None
    many = "  [변경] a\n  (서버와 같은 파일 150개는 apply 때 건드리지 않습니다)\n[정보] 신규 0 / 변경 1 / 동일 150 / 삭제예정 0 / 이미없음 0 / 정리예정 0\n"
    r = remote.parse_check(many)                                 # 파일이 많으면 '동일' 은 개수만
    assert r.counts["동일"] == 150 and r.paths("동일") == [] and r.changes_server == 1
    assert remote.parse_check("  [변경] a\n  [변경] b\n").counts["변경"] == 2   # 요약 줄이 없으면 센다


def test_parse_server_history():
    text = ("2026-10-01 10:00:00\tdeployer\tbackup\t/srv/app\t2026-10-01-0900\t보관 2개\n"
            "2026-10-01 10:01:00\tdeployer\tapply\t/srv/app\t2026-10-01-0900\t반영 2개, 동일(건너뜀) 0개\r\n"
            "2026-10-01 10:05:00\tdeployer\tapply\t/srv/other\t2026-10-01-1000\t반영 1개\n"
            "깨진 줄\n\n")
    entries = remote.parse_history(text)
    assert [e["action"] for e in entries] == ["backup", "apply", "apply"]
    assert entries[1]["result"] == "반영 2개, 동일(건너뜀) 0개" and entries[1]["package"] == "2026-10-01-0900"
    assert remote.current_package(entries, "/srv/app")["package"] == "2026-10-01-0900"   # backup 은 서버를 안 바꿈
    assert remote.current_package(entries, "/srv/none") is None
    entries.append(dict(zip(remote.HISTORY_FIELDS, ["t", "u", "rollback", "/srv/app", "2026-10-01-0900", "복구 2개"])))
    assert remote.current_package(entries, "/srv/app")["action"] == "rollback"


@needs_bash
def test_fetch_history_from_server(sshd, tmp_path, maven_project):
    server = tmp_path / "server"
    write(server / "WEB-INF/views/user/list.jsp", "OLD")
    log = tmp_path / "home/.shipkit/history.log"
    project = make_config(maven_project, tmp_path / "out", server_path=bash_path(server),
                          server={"history_file": bash_path(log)}).project("erp")
    changes = [FileChange("src/main/webapp/WEB-INF/views/user/list.jsp", C.MODIFIED, "!1")]
    result = packager.build_package(project, mapper.plan(project, changes), SourceInfo("manual", "t"),
                                    source_changes=changes, now=datetime(2026, 10, 30, 9, 0))
    p = _profile(sshd)
    conn = _connect(sshd, tmp_path, p, remote.Secrets(login=LOGIN_PW))
    out = []
    try:
        assert remote.fetch_history(conn, p, remote.Secrets(), bash_path(log), out.append) == []   # 아직 이력 없음
        res = remote.upload_and_check(conn, p, remote.Secrets(), result.out_dir, out.append)
        assert res.ok and remote.backup_and_apply(conn, p, remote.Secrets(), res.remote_dir, out.append, False).ok
        entries = remote.fetch_history(conn, p, remote.Secrets(), bash_path(log), out.append)
        assert [e["action"] for e in entries] == ["backup", "apply"] and entries[-1]["package"] == result.out_dir.name
        assert remote.current_package(entries, bash_path(server))["action"] == "apply"
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
