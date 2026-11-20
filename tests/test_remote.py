"""서버에 바로 올리고 실행하기: 테스트용 SSH 서버에 실제 SSHConnection 으로 접속해서 확인한다."""
from datetime import datetime
from pathlib import Path

import shlex

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
         "key_file": "C:/keys/id_ed25519", "server_path": "/srv/prod", "branch": "release/1.0",
         "log_file": "/var/log/app/catalina.out"}])}}).project("a")
    dev, prod = p.servers
    assert (dev.runner, dev.run_with, dev.chown, dev.upload_dir) == ("app", "su", True, "~/deploy")
    assert prod.address == "app@prod.acme.local:2222" and prod.server_path == "/srv/prod" and prod.runner == "app"
    assert prod.branch == "release/1.0" and dev.branch == ""
    assert prod.log_file == "/var/log/app/catalina.out" and dev.log_file == ""
    assert remote.tail_command(prod) == "tail -n 200 -F /var/log/app/catalina.out"
    win = ServerProfile(name="w", host="h", user="u", server_os="windows", log_file="D:/tomcat/logs/catalina.out")
    assert remote.tail_command(win) == "Get-Content -Path 'D:\\tomcat\\logs\\catalina.out' -Tail 200 -Wait"
    for bad, msg in [({"name": "x", "host": "h"}, "user"), ({"name": "x", "host": "h", "user": "u", "auth": "key"}, "key_file"),
                     ({"name": "x", "host": "h; rm", "user": "u"}, "host"),
                     ({"name": "x", "host": "h", "user": "u", "upload_dir": "deploy"}, "upload_dir"),
                     ({"name": "x", "host": "h", "user": "u", "branch": "ma ster"}, "branch"),
                     ({"name": "x", "host": "h", "user": "u", "log_file": "logs/app.log"}, "log_file"),
                     ({"name": "x", "host": "h", "user": "u", "log_file": "/var/log/$x"}, "log_file")]:
        with pytest.raises(ConfigError, match=msg):
            parse_config({"projects": {"a": dict(base, servers=[bad])}})


def test_notify_config_compose_and_send(monkeypatch):
    """메신저 알림: 서버 연결의 notify 설정 검증, 형식별 JSON 키, 메시지 글, 웹훅 전송(성공·거부·네트워크 오류)."""
    from shipkit import notify
    from conftest import FakeResponse
    base = {"root": "x", "output_dir": "y", "server_path": "/srv/app"}

    def server(**notify_cfg):
        return parse_config({"projects": {"a": dict(base, servers=[
            {"name": "운영", "host": "h", "user": "u", "notify": notify_cfg}])}}).project("a").servers[0]

    plain = parse_config({"projects": {"a": dict(base, servers=[{"name": "운영", "host": "h", "user": "u"}])}}).project("a")
    assert plain.servers[0].notify is None
    slack = server(url="https://hooks.slack.com/services/T/B/x").notify           # 형식을 비우면 slack
    assert (slack.format, slack.field, slack.label) == ("slack", "text", "Slack")
    assert server(url="https://jandi.example/x", format="jandi").notify.field == "body"
    assert server(url="https://discord.com/api/webhooks/x", format="Discord").notify.field == "content"
    custom = server(url="http://10.0.0.9/hook", format="custom", key="message").notify
    assert custom.field == "message" and custom.label == "직접 입력"
    for bad, msg in [({"format": "slack"}, "notify.url"), ({"url": "hooks.slack.com/x"}, "notify.url"),
                     ({"url": "https://h/x", "format": "kakao"}, "notify.format"),
                     ({"url": "https://h/x", "format": "custom"}, "notify.key"),
                     ({"url": "https://h/x", "format": "custom", "key": "my-key"}, "notify.key"),
                     ({"url": "https://h/x", "detail": "all"}, "notify.detail")]:
        with pytest.raises(ConfigError, match=msg):
            server(**bad)
    assert slack.detail == "none" and server(url="https://h/x", detail="Files").notify.detail == "files"

    # 변경 내역: MR·PR 제목 한 줄씩(작성자), 파일 목록 한 줄, 많으면 '외 N개'. MR 없는 패키지는 이름(설명). 되돌리기에는 안 붙음
    from shipkit.history import PackageRecord
    rec = PackageRecord(Path("p"), datetime(2026, 11, 20), "gitlab-mr", "MR 2개", applied=["a.jsp", "b.jsp"], deletes=["c.jsp"],
                        requests=[{"iid": 612, "ref": "!612", "title": "에디터 호환성", "author": "kim"},
                                  {"iid": 615, "ref": "!615", "title": "첨부 크기 제한", "author": ""}])
    assert notify.detail_lines(rec, "none") == [] and notify.detail_lines(None, "files") == []
    assert notify.detail_lines(rec, "requests") == ["• !612 에디터 호환성 (kim)", "• !615 첨부 크기 제한"]
    assert notify.detail_lines(rec, "files") == ["• !612 에디터 호환성 (kim)", "• !615 첨부 크기 제한", "파일: a.jsp, b.jsp, 삭제 c.jsp"]
    plain_rec = PackageRecord(Path("p"), datetime(2026, 11, 20), "git-diff", "git 범위 v1..HEAD", applied=[f"f{i}" for i in range(17)],
                              label="핫픽스")
    assert notify.detail_lines(plain_rec, "files") == ["• 핫픽스", "파일: " + ", ".join(f"f{i}" for i in range(15)) + " … 외 2개"]
    many = PackageRecord(Path("p"), datetime(2026, 11, 20), "gitlab-mr", "", requests=[{"ref": f"!{i}", "title": "t"} for i in range(25)])
    lines = notify.detail_lines(many, "requests")
    assert len(lines) == 21 and lines[-1] == "• … 외 5개"
    assert notify.compose("erp", "운영", "apply", "p", True, files=3, sender="", details=["• !1 x", "파일: a"]) == \
        "✅ erp 운영에 p 반영, 파일 3개\n• !1 x\n파일: a"
    assert notify.compose("erp", "운영", "rollback", "p", True, sender="", details=["• !1 x"]) == "↩️ erp 운영 되돌리기: p (반영 전 상태로)"

    assert notify.compose("erp", "운영", "apply", "2026-11-04-1430", True, files=12, sender="mina") == \
        "✅ erp 운영에 2026-11-04-1430 반영, 파일 12개 (mina)"
    assert notify.compose("erp", "운영", "backup", "p", False, code=3, sender="") == "❌ erp 운영 반영 실패 (백업, 종료 코드 3): p"
    assert notify.compose("erp", "운영", "rollback", "p", True, sender="") == "↩️ erp 운영 되돌리기: p (반영 전 상태로)"
    assert notify.compose("erp", "운영", "rollback", "p", False, code=1, sender="") == "❌ erp 운영 되돌리기 실패 (종료 코드 1): p"
    if notify.who():                                                              # 보낸 사람은 이 PC 의 로그인 이름
        assert notify.compose("erp", "운영", "apply", "p", True).endswith(f"({notify.who()})")

    posts, responses = [], [FakeResponse(200, "ok")]

    def fake_post(url, json=None, timeout=None):
        posts.append((url, json, timeout))
        return responses.pop(0)

    monkeypatch.setattr("requests.post", fake_post)
    notify.send(slack, "hi")
    assert posts == [("https://hooks.slack.com/services/T/B/x", {"text": "hi"}, notify.TIMEOUT)]
    responses.append(FakeResponse(403, "invalid_token"))
    with pytest.raises(notify.NotifyError, match="HTTP 403.*invalid_token"):
        notify.send(custom, "hi")
    assert posts[-1][1] == {"message": "hi"}
    import requests

    def refused(*_a, **_k):
        raise requests.ConnectionError("refused")

    monkeypatch.setattr("requests.post", refused)
    with pytest.raises(notify.NotifyError, match="보내지 못했습니다.*refused"):
        notify.send(slack, "x")


def write_ssh_key(path: Path, passphrase: str = "") -> Path:
    """테스트용 OpenSSH 형식 ed25519 개인 키 파일 (passphrase 가 있으면 암호로 잠근다)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519
    encryption = (serialization.BestAvailableEncryption(passphrase.encode()) if passphrase
                  else serialization.NoEncryption())
    path.write_bytes(ed25519.Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH, encryption))
    return path


def test_key_needs_passphrase(tmp_path):
    """SSH 키 파일의 암호 여부. paramiko 5 는 암호 걸린 OpenSSH 키에 PasswordRequiredException 대신 TypeError 를 내므로
    그것도 '암호 필요'로 본다 (안 그러면 연결 시험이 예외로 끊겨 단추가 '시험 중…' 에 멈춘다)."""
    plain = write_ssh_key(tmp_path / "plain")
    locked = write_ssh_key(tmp_path / "locked", "secret")
    assert remote.key_needs_passphrase(str(plain)) is False
    assert remote.key_needs_passphrase(str(locked)) is True
    with pytest.raises(remote.RemoteError, match="없습니다"):
        remote.key_needs_passphrase(str(tmp_path / "nope"))
    (tmp_path / "junk").write_text("not a key", encoding="utf-8")
    with pytest.raises(remote.RemoteError, match="읽지 못했습니다"):
        remote.key_needs_passphrase(str(tmp_path / "junk"))
    # 어떤 비밀번호를 물을지: 암호 걸린 키면 키 암호 하나, 아니면 묻지 않는다
    slots = remote.secret_slots(ServerProfile("개발", "h", "harami", port=1979, auth="key", key_file=str(locked)))
    assert [(s[0], s[1]) for s in slots] == [("login", "ssh:passphrase:harami@h:1979")] and "SSH 키 암호" in slots[0][2]
    assert remote.secret_slots(ServerProfile("개발", "h", "harami", auth="key", key_file=str(plain))) == []


def test_commands_for_each_account_mode():
    plain = ServerProfile("a", "h", "app")
    assert remote.script_command(plain, "/home/app/deploy/p", "check") == \
        "bash -c 'cd /home/app/deploy/p && bash shipkit.sh check'"
    sudo = ServerProfile("a", "h", "admin", run_as="app", run_password=True, chown=True)
    assert remote.script_command(sudo, "/d/p", "apply", yes=True).startswith("sudo -S -p '' -u app bash -c ")
    assert remote.chown_command(sudo, "/d/p") == "sudo -S -p '' chown -R app: /d/p"
    su = ServerProfile("a", "h", "root", run_as="app", run_with="su", server_path="/srv/other")
    assert remote.script_command(su, "/d/p", "rollback", no_hooks=True) == \
        "LC_ALL=C su - app -c 'cd /d/p && bash shipkit.sh rollback /srv/other --no-hooks'"
    assert remote.chown_command(su, "/d/p") is None                      # chown 안 켬
    # 비밀번호는 설정에서 켰을 때만 묻는다: chown 을 켜도(sudo 가 필요해도) 끄면 sudo -n 으로 해 보고 거부될 때 안내
    nopw_chown = ServerProfile("a", "h", "cloud-user", run_as="harami", chown=True)
    assert [k for k, _key, _text in remote.secret_slots(nopw_chown)] == ["login"]
    assert remote.chown_command(nopw_chown, "/d/p") == "sudo -n chown -R harami: /d/p"
    assert remote.run_password(nopw_chown, remote.Secrets(), "chown") == {"password": None, "pty": False}
    assert [k for k, _key, _text in remote.secret_slots(sudo)] == ["login", "run"]
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


def test_password_prompt_detection():
    """su·PAM 의 암호 질문을 로케일에 상관없이 알아본다. 평범한 출력은 질문으로 보지 않는다."""
    for prompt in ("Password: ", "Password:", "[sudo] password for admin: ", "암호: ", "암호:", "비밀번호: ",
                   "パスワード: ", "パスワード：", "密码：", "Passwort: ", "Mot de passe : ", "Contraseña: ", "Пароль: ",
                   "Senha: ", "some output\r\nPassword: "):
        assert remote._is_password_prompt(prompt), prompt
    for text in ("", "Password", "password changed\n", "Password: ok\n", "암호를 바꾸세요\n", "hello:\n",
                 "password is required\nnext line:"):
        assert not remote._is_password_prompt(text), text
    assert remote._sudo_refused("sudo: a password is required\n")
    assert remote._sudo_refused("sudo: 암호가 필요합니다\r\n")
    assert not remote._sudo_refused("sudo: 암호가 필요합니다\nmore output\n")   # 안쪽 명령까지 실행된 흔적이 있으면 아니다
    assert not remote._sudo_refused("[오류] 파일이 없습니다\n")


@needs_bash
def test_run_as_on_localized_server(sshd, tmp_path, maven_project):
    """한국어·일본어 로케일 서버: su 는 LC_ALL=C 로 영어 질문을 받고, LC_ALL 을 무시하는 서버의 번역된 질문도 알아챈다.
    sudo -n 거부 문구가 번역되어도 안내를 붙인다."""
    server = tmp_path / "server"
    server.mkdir()
    result = _package(maven_project, tmp_path, server)
    su = _profile(sshd, user="admin", run_as="app", run_with="su", run_password=True)
    for locale, honor_env in (("ko", True), ("ko", False), ("ja", False)):
        sshd.locale, sshd.honor_env, sshd.ran_as[:] = locale, honor_env, []
        conn = _connect(sshd, tmp_path, su, remote.Secrets(login=LOGIN_PW))
        out = []
        try:
            res = remote.upload_and_check(conn, su, remote.Secrets(LOGIN_PW, SU_PW), result.out_dir, out.append)
            text = "".join(out)
            assert res.ok and sshd.ran_as == ["su:app"], (locale, honor_env, text)
            assert "Password" not in text and "암호" not in text and "パスワード" not in text, text
        finally:
            conn.close()
    sshd.locale, sshd.honor_env = "C", True

    sshd.locale = "ko"
    sudo = _profile(sshd, user="admin", run_as="app")
    conn = _connect(sshd, tmp_path, sudo, remote.Secrets(login=LOGIN_PW))
    out = []
    try:
        res = remote.upload_and_check(conn, sudo, remote.Secrets(LOGIN_PW), result.out_dir, out.append)
        text = "".join(out)
        assert not res.ok and "sudo: 암호가 필요합니다" in text and "run_password" in text, text
    finally:
        conn.close()
        sshd.locale = "C"


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
        res = remote.upload_and_check(conn, sudo, remote.Secrets(LOGIN_PW), result.out_dir, out.append)  # 거부되면 아무것도 안 올라가므로 올리기부터
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
def test_entry_perm_owner():
    """목록 항목의 권한 글자·숫자와 소유자: longname(ls -l 식)이 있으면 이름, 없으면 uid·gid 숫자, 아무것도 모르면 빈 칸."""
    from types import SimpleNamespace
    e = remote.RemoteEntry("a.sh", "/x/a.sh", False, mode=0o100755, owner="app", group="web")
    assert (e.perm, e.octal, e.owner_text) == ("-rwxr-xr-x", "755", "app:web")
    d = remote.RemoteEntry("d", "/x/d", True, mode=0o040750, owner="root", group="")
    assert (d.perm, d.octal, d.owner_text) == ("drwxr-x---", "750", "root:")
    assert remote.RemoteEntry("?", "/?", False).perm == "" and remote.RemoteEntry("?", "/?", False).owner_text == ""
    named = SimpleNamespace(longname="-rw-r--r--    1 app      web          1234 Nov 20 10:00 a.sh", st_uid=1001, st_gid=1002)
    assert remote._owner_group(named) == ("app", "web")
    assert remote._owner_group(SimpleNamespace(longname="", st_uid=1001, st_gid=1002)) == ("1001", "1002")
    assert remote._owner_group(SimpleNamespace(st_uid=None, st_gid=None)) == ("", "")


def test_read_write_file_and_text_codec(sshd, tmp_path):
    """편집기용 파일 읽기·쓰기: 크기 한도·이진 파일 거부, 저장 전 .bak, 인코딩·줄 끝 알아내고 되돌리기."""
    from sshd import LOGIN_PW
    home = sshd.home
    (home / "a.conf").write_bytes("key=값\r\nnext=1\r\n".encode("cp949"))
    (home / "big.txt").write_bytes(b"x" * 20)
    (home / "bin.dat").write_bytes(b"ab\x00cd")
    p = _profile(sshd)
    conn = _connect(sshd, tmp_path, p, remote.Secrets(login=LOGIN_PW))
    try:
        base = conn.home()
        data = conn.read_file(f"{base}/a.conf", limit=1000)
        text, enc, nl = remote.decode_text(data, ["utf-8", "cp949"])
        assert (text, enc, nl) == ("key=값\nnext=1\n", "cp949", "\r\n")
        with pytest.raises(remote.RemoteError, match="큽니다"):
            conn.read_file(f"{base}/big.txt", limit=10)
        with pytest.raises(remote.RemoteError, match="이진 파일"):
            conn.read_file(f"{base}/bin.dat")
        with pytest.raises(remote.RemoteError, match="그런 경로가 없습니다"):
            conn.read_file(f"{base}/nope.txt")
        new = remote.encode_text(text + "added=2\n", enc, nl)
        assert conn.write_file(f"{base}/a.conf", new, backup=data) == f"{base}/a.conf.bak"
        assert (home / "a.conf").read_bytes() == "key=값\r\nnext=1\r\nadded=2\r\n".encode("cp949")
        assert (home / "a.conf.bak").read_bytes() == data
        assert conn.write_file(f"{base}/a.conf", b"plain\n") == "" and (home / "a.conf").read_bytes() == b"plain\n"
    finally:
        conn.close()
    assert remote.decode_text(b"\xef\xbb\xbfhi\n") == ("hi\n", "utf-8-sig", "\n")
    assert remote.encode_text("hi\n", "utf-8-sig", "\n") == b"\xef\xbb\xbfhi\n"
    assert remote.decode_text(b"\xff\xfe junk", ["utf-8"])[1] == "utf-8"            # 어느 것으로도 안 읽히면 깨진 글자로
    assert remote.decode_text(b"a\nb", ["utf-8", "cp949"]) == ("a\nb", "utf-8", "\n")


def test_diagnose(sshd, tmp_path):
    """연결 진단: 접속·실행 계정 전환·올릴 위치·배포 경로·명령·로그 파일을 확인하고, 설정으로 고칠 것은 추천한다."""
    from sshd import LOGIN_PW, SUDO_PW
    target = tmp_path / "target"
    target.mkdir()

    def run(profile, secrets=None, server_path=bash_path(target)):
        secrets = secrets or remote.Secrets(login=LOGIN_PW)
        conn = _connect(sshd, tmp_path, profile, secrets)
        try:
            return remote.diagnose(conn, profile, secrets, server_path)
        finally:
            conn.close()

    d = run(_profile(sshd))
    assert d.ok and [c.state for c in d.checks] == ["ok"] * 5 and d.suggestions == {}
    assert d.summary().startswith(f"접속 성공: deployer@127.0.0.1:{sshd.port}\n✓ 접속: ")
    assert [c.name for c in d.checks] == ["접속", "실행 계정", "올릴 위치", "배포 경로", "필요한 명령"] and "deploy" in d.checks[2].text
    d = run(_profile(sshd), server_path=bash_path(tmp_path / "nope"))          # 배포 경로 없음
    assert not d.ok and d.checks[3].state == "fail" and "폴더가 없습니다" in d.checks[3].text
    d = run(_profile(sshd, log_file="/var/log/nope.log"))                       # 로그 파일은 없어도 경고만
    assert d.ok and d.checks[-1].name == "로그 파일" and d.checks[-1].state == "warn"

    d = run(_profile(sshd, run_as="app", run_with="sudo"))                      # sudo 에 비밀번호가 필요 -> '비밀번호 필요' 켜라고 추천
    fail = d.checks[1]
    assert not d.ok and fail.state == "fail" and "비밀번호가 필요합니다" in fail.fix and d.suggestions == {"run_password": True}
    assert d.checks[2].state == "info" and d.checks[3].state == "info"         # 전환이 안 되면 올릴 위치·배포 경로는 못 본다
    sshd.sudo_nopasswd = True
    d = run(_profile(sshd, run_as="app", run_with="sudo"))                      # NOPASSWD: 그대로 정상, 올릴 위치도 실행 계정으로 확인
    assert d.ok and d.checks[1].state == "ok" and "비밀번호 없이" in d.checks[1].text and d.suggestions == {}
    assert d.checks[2].state == "ok" and "(app 계정이 씀)" in d.checks[2].text
    d = run(_profile(sshd, run_as="app", run_with="sudo", run_password=True), remote.Secrets(login=LOGIN_PW, run=SUDO_PW))
    assert d.ok and d.checks[1].state == "warn" and d.suggestions == {"run_password": False}   # 비밀번호 없이도 되니 꺼도 된다
    d = run(_profile(sshd, run_as="app", run_with="su"))                        # su 는 비밀번호 필요, sudo 는 없이 됨 -> sudo 로 바꾸라고
    assert not d.ok and d.checks[1].state == "fail" and d.suggestions == {"run_with": "sudo", "run_password": False, "chown": False}
    sshd.sudo_nopasswd = False
    d = run(_profile(sshd, run_as="app", run_with="su"))                        # su 비밀번호 없음, sudo 도 비밀번호 필요
    assert d.checks[1].state == "fail" and d.suggestions == {"run_password": True} and "sudo -u 계정" in d.checks[1].fix


def test_host_key_new_known_changed(sshd, tmp_path):
    known = tmp_path / "known_hosts"
    p = _profile(sshd)
    status = remote.check_host_key(p, known)
    assert status.state == "new" and status.fingerprint.startswith("SHA256:")
    remote.remember_host_key(p, status, known)
    assert remote.check_host_key(p, known).state == "known" and remote.has_host_key(p, known)
    sshd.key = __import__("paramiko").RSAKey.generate(1024)             # 서버를 다시 설치한 경우
    assert remote.check_host_key(p, known).state == "changed"
    assert remote.forget_host_key(p, known)                             # 지문 잊기 -> 처음 접속처럼 다시 묻는다
    assert not remote.has_host_key(p, known) and not remote.has_host_key(p, tmp_path / "none")
    assert remote.check_host_key(p, known).state == "new"
    assert not remote.forget_host_key(p, known)                         # 지울 것이 없으면 False
    assert not remote.forget_host_key(p, tmp_path / "none")


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
        # sudo 면 실행 계정으로 바로 올린다 (비밀번호는 sudo -S 첫 줄로): 홈 조회·올리기·check 모두 실행 계정, chown 없음
        assert sshd.ran_as[-3:] == ["sudo:app"] * 3 and not any(c.startswith("sudo -S -p '' chown") for c in sshd.commands)
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


def test_windows_server_profile_and_commands():
    """Windows 서버 연결: 설정 검증과 PowerShell 명령 조립."""
    base = {"root": "x", "output_dir": "y", "server_os": "windows", "server_path": "D:/tomcat/webapps/ROOT"}
    p = parse_config({"projects": {"a": dict(base, servers=[
        {"name": "운영", "host": "10.0.0.7", "user": "deployer", "upload_dir": "D:\\deploy\\", "server_path": "E:/web"}])}}).project("a")
    s = p.servers[0]
    assert s.server_os == "windows" and s.upload_dir == "D:/deploy" and s.server_path == "E:\\web"
    for bad, msg in [({"name": "x", "host": "h", "user": "u", "run_as": "svc"}, "Windows 서버에서는"),
                     ({"name": "x", "host": "h", "user": "u", "chown": True}, "Windows 서버에서는"),
                     ({"name": "x", "host": "h", "user": "u", "upload_dir": "deploy"}, "upload_dir"),
                     ({"name": "x", "host": "h", "user": "u", "upload_dir": "D:/dep$loy"}, "upload_dir")]:
        with pytest.raises(ConfigError, match=msg):
            parse_config({"projects": {"a": dict(base, servers=[bad])}})

    assert remote.remote_package_dir(s, "p", "/C:/Users/deployer") == "/D:/deploy/p"
    home = ServerProfile("a", "h", "u", server_os="windows")                     # upload_dir 기본값 ~/deploy
    assert remote.remote_package_dir(home, "p", "/C:/Users/deployer") == "/C:/Users/deployer/deploy/p"
    assert remote.to_windows_path("/C:/Users/deployer/deploy/p") == "C:\\Users\\deployer\\deploy\\p"
    assert remote.to_windows_path("C:/x/y") == "C:\\x\\y"

    cmd = remote.script_command(s, "/D:/deploy/p", "apply", yes=True, no_hooks=False)
    assert cmd.startswith("powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand ")
    code = remote.decode_powershell_command(cmd)
    assert code == ("[Console]::OutputEncoding = [Text.Encoding]::UTF8; & 'D:\\deploy\\p\\shipkit.ps1' 'apply' 'E:\\web' -Yes; "
                    "exit $LASTEXITCODE")
    assert remote.chown_command(s, "/D:/deploy/p") is None
    assert remote.run_password(s, remote.Secrets(run="pw")) == {"password": None, "pty": False}
    assert remote.secret_slots(s) == [("login", "ssh:login:deployer@10.0.0.7:22", "deployer@10.0.0.7 접속 비밀번호")]


@needs_bash
def test_windows_server_deploy_end_to_end(sshd, tmp_path, maven_project):
    """Windows 서버 흉내: 테스트 SSH 서버가 받은 powershell 명령을 이 PC 의 PowerShell 로 실제 실행한다."""
    from test_windows_server import needs_powershell  # noqa: F401
    import shutil
    if shutil.which("powershell") is None:
        pytest.skip("PowerShell 이 없습니다")
    server = tmp_path / "server root"
    write(server / "WEB-INF/views/user/list.jsp", "OLD")
    history_log = tmp_path / "home/.shipkit/history.log"
    project = make_config(maven_project, tmp_path / "out", server_os="windows", server_path=str(server),
                          server={"history_file": str(history_log)}).project("erp")
    changes = [FileChange("src/main/webapp/WEB-INF/views/user/list.jsp", C.MODIFIED, "!1")]
    result = packager.build_package(project, mapper.plan(project, changes), SourceInfo("manual", "t"),
                                    source_changes=changes, now=datetime(2026, 10, 30, 9, 0))
    assert (result.out_dir / "shipkit.ps1").is_file()
    p = _profile(sshd, server_os="windows")                           # upload_dir 기본값 ~/deploy
    conn = _connect(sshd, tmp_path, p, remote.Secrets(login=LOGIN_PW))
    out = []
    try:
        res = remote.upload_and_check(conn, p, remote.Secrets(), result.out_dir, out.append)
        assert res.ok, "".join(out)
        assert (sshd.home / "deploy" / result.out_dir.name / "shipkit.ps1").is_file()
        checked = remote.parse_check("".join(out))
        assert checked is not None and checked.paths("변경") == ["WEB-INF/views/user/list.jsp"], "".join(out)
        res = remote.backup_and_apply(conn, p, remote.Secrets(), res.remote_dir, out.append, run_hooks=False)
        assert res.ok and res.action == "apply", "".join(out)
        assert (server / "WEB-INF/views/user/list.jsp").read_text(encoding="utf-8") == "<%-- jsp --%>"
        entries = remote.fetch_history(conn, p, remote.Secrets(), str(history_log), out.append)
        assert [e["action"] for e in entries] == ["backup", "apply"]
        assert remote.current_package(entries, str(server))["package"] == result.out_dir.name
        assert remote.run_action(conn, p, remote.Secrets(), res.remote_dir, "rollback", out.append, no_hooks=True).ok
        assert (server / "WEB-INF/views/user/list.jsp").read_text(encoding="utf-8") == "OLD"
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


def test_parse_cwd_output():
    assert remote.parse_cwd_output("R 4321\nC /home/app/deploy\n") == (4321, "/home/app/deploy")
    assert remote.parse_cwd_output("R 0\nC \n") == (0, "")
    assert remote.parse_cwd_output("R x\n") == (0, "")
    assert remote.parse_cwd_output("") == (0, "")


@needs_bash
def test_cwd_script_finds_tagged_shell(tmp_path):
    """터미널 따라가기 스크립트: 표식(환경 변수)을 심은 셸 프로세스를 /proc 에서 찾아 그 현재 폴더를 돌려준다.
    (Git Bash 의 /proc 에도 environ·stat·cwd 가 있어 여기서도 돈다.)"""
    import os
    import subprocess
    import time
    if subprocess.run([BASH, "-c", "test -r /proc/self/environ"]).returncode != 0:
        pytest.skip("/proc 이 없는 환경")
    folder = tmp_path / "work dir"
    folder.mkdir()
    tag = "t3st12"
    proc = subprocess.Popen([BASH, "-c", "sleep 30"], env=dict(os.environ, LC_SHIPKIT_SESSION=tag), cwd=str(folder),
                            stdout=subprocess.DEVNULL)
    try:
        time.sleep(0.3)

        def run(pid):
            script = remote.CWD_SCRIPT % {"tag": shlex.quote(tag), "pid": pid}
            return remote.parse_cwd_output(subprocess.run([BASH, "-c", script], capture_output=True, timeout=60).stdout.decode())

        found, cwd = run(0)                                   # 처음: /proc 전체에서 표식으로 찾는다
        assert found > 0 and cwd.endswith("/work dir"), (found, cwd)
        assert run(found) == (found, cwd)                     # PID 를 알면 바로 그 프로세스를 본다
        assert run(0)[0] == found
    finally:
        proc.kill()


@needs_bash
def test_upload_as_runner_streams_tar(sshd, tmp_path, maven_project):
    """다른 계정 + sudo: SFTP 대신 패키지를 tar 로 묶어 'sudo -u 계정 tar' 에 흘려 넣는다
    (접속 계정의 쓰기 권한·chown 이 필요 없다). sudo 가 비밀번호를 요구하면 거부 안내를 붙인다."""
    server = tmp_path / "server"
    server.mkdir()
    result = _package(maven_project, tmp_path, server)
    sshd.sudo_nopasswd = True
    p = _profile(sshd, user="cloud-user", run_as="app", upload_dir=bash_path(tmp_path / "home/deploy"))
    conn = _connect(sshd, tmp_path, p, remote.Secrets(login=LOGIN_PW))
    out = []
    try:
        res = remote.upload_and_check(conn, p, remote.Secrets(LOGIN_PW), result.out_dir, out.append)
        assert res.ok, "".join(out)
        assert sshd.ran_as == ["sudo:app", "sudo:app"]                     # 올리기·check 모두 실행 계정
        assert any("tar xzf -" in c for c in sshd.commands) and not any("chown" in c for c in sshd.commands)
        uploaded = tmp_path / "home/deploy" / result.out_dir.name
        assert (uploaded / "shipkit.sh").is_file()
        assert (uploaded / "manifest.txt").read_bytes() == (result.out_dir / "manifest.txt").read_bytes()
    finally:
        conn.close()
    sshd.sudo_nopasswd = False                                             # NOPASSWD 가 아닌 서버: 거부 + 안내
    conn = _connect(sshd, tmp_path, p, remote.Secrets(login=LOGIN_PW))
    out = []
    try:
        res = remote.upload_and_check(conn, p, remote.Secrets(LOGIN_PW), result.out_dir, out.append)
        assert not res.ok and res.action == "upload" and "sudo 에 비밀번호가 필요합니다" in "".join(out)
    finally:
        conn.close()


def test_package_archive_modes(tmp_path):
    """tar 로 묶을 때 권한은 리눅스 기준: 서버 스크립트·hooks/*.sh 755, 나머지 644."""
    import tarfile
    pkg = tmp_path / "pkg"
    write(pkg / "shipkit.sh", "#!/bin/bash")
    write(pkg / "hooks/pre.sh", "x")
    write(pkg / "files/a.txt", "a")
    path = remote.package_archive(pkg)
    try:
        with tarfile.open(path) as tar:
            modes = {m.name: m.mode for m in tar.getmembers()}
        assert modes == {"files/a.txt": 0o644, "hooks/pre.sh": 0o755, "shipkit.sh": 0o755}
    finally:
        path.unlink()
    assert remote.upload_command(ServerProfile("a", "h", "cloud-user", run_as="app"), "/opt/d/p") == \
        "sudo -n -u app bash -c 'mkdir -p /opt/d/p && tar xzf - -C /opt/d/p'"
    assert remote.stream_upload(ServerProfile("a", "h", "cloud-user", run_as="app"))
    assert not remote.stream_upload(ServerProfile("a", "h", "cloud-user", run_as="app", run_with="su"))
    assert not remote.stream_upload(ServerProfile("a", "h", "cloud-user"))
