"""명령어로 서버에 바로 반영: shipkit deploy / server-status 를 테스트용 SSH 서버에 실제로 실행한다."""
import json
from pathlib import Path

import pytest
import yaml

pytest.importorskip("paramiko")

from shipkit import cli, history, remote_cli  # noqa: E402
from shipkit.config import parse_config  # noqa: E402

from conftest import write  # noqa: E402
from sshd import LOGIN_PW  # noqa: E402
from test_remote import bash_path, needs_bash, sshd  # noqa: E402,F401
from test_gui import FakeKeyring  # noqa: E402


def _setup(tmp_path, maven_project, sshd, monkeypatch):
    keyring = FakeKeyring()
    monkeypatch.setattr("shipkit.secrets._keyring", lambda: keyring)
    monkeypatch.setattr("shipkit.remote.known_hosts_path", lambda: tmp_path / "known_hosts")
    monkeypatch.setenv(remote_cli.ENV_LOGIN, LOGIN_PW)
    server = tmp_path / "server"
    write(server / "WEB-INF/views/user/list.jsp", "OLD")
    cfg = tmp_path / "shipkit.yaml"
    cfg.write_text(yaml.safe_dump({"projects": {"erp": {
        "root": maven_project.as_posix(), "output_dir": (tmp_path / "out").as_posix(),
        "server_path": bash_path(server), "server": {"history_file": bash_path(tmp_path / "home/.shipkit/history.log")},
        "servers": [{"name": "개발", "host": "127.0.0.1", "port": sshd.port, "user": "deployer"}]}}}), encoding="utf-8")
    return cfg, server, keyring


def test_find_server_and_package(tmp_path):
    base = {"root": str(tmp_path), "output_dir": str(tmp_path / "out"), "server_path": "/srv/app"}
    none = parse_config({"projects": {"a": base}}).project("a")
    with pytest.raises(remote_cli.DeployError, match="서버 연결"):
        remote_cli.find_server(none, None)
    two = parse_config({"projects": {"a": dict(base, servers=[{"name": "개발", "host": "h", "user": "u"},
                                                              {"name": "운영", "host": "h2", "user": "u"}])}}).project("a")
    with pytest.raises(remote_cli.DeployError, match="--server"):
        remote_cli.find_server(two, None)
    assert remote_cli.find_server(two, "운영").host == "h2"
    with pytest.raises(remote_cli.DeployError, match="설정에 없는 서버"):
        remote_cli.find_server(two, "없음")
    with pytest.raises(remote_cli.DeployError, match="만든 패키지가 없습니다"):
        remote_cli.find_package(two, None)
    with pytest.raises(remote_cli.DeployError, match="패키지 폴더가 아닙니다"):
        remote_cli.find_package(two, str(tmp_path))


@needs_bash
def test_deploy_check_apply_rollback_and_status(tmp_path, maven_project, sshd, monkeypatch, capsys):
    cfg, server, keyring = _setup(tmp_path, maven_project, sshd, monkeypatch)
    jsp = "src/main/webapp/WEB-INF/views/user/list.jsp"
    assert cli.main(["package", "-c", str(cfg), "-p", "erp", jsp]) == 0
    capsys.readouterr()

    # 처음 접속: 터미널이 아니면 지문을 자동으로 기억하지 않는다
    assert cli.main(["deploy", "-c", str(cfg), "-p", "erp"]) == 2
    assert "--accept-host-key" in capsys.readouterr().err

    assert cli.main(["deploy", "-c", str(cfg), "-p", "erp", "--accept-host-key"]) == 0   # check 만
    out = capsys.readouterr().out
    assert "지문을 기억했습니다" in out and "apply 때 바뀌는 파일 1개 (변경 1)" in out and "확인만 했습니다" in out
    assert (server / "WEB-INF/views/user/list.jsp").read_text(encoding="utf-8") == "OLD"

    assert cli.main(["deploy", "-c", str(cfg), "-p", "erp", "--apply"]) == 2             # hooks 결정이 없으면 멈춤
    assert "--hooks 또는 --no-hooks" in capsys.readouterr().err
    assert cli.main(["deploy", "-c", str(cfg), "-p", "erp", "--apply", "--no-hooks"]) == 0
    assert "반영했습니다" in capsys.readouterr().out
    assert (server / "WEB-INF/views/user/list.jsp").read_text(encoding="utf-8") == "<%-- jsp --%>"

    assert cli.main(["server-status", "-c", str(cfg), "-p", "erp"]) == 0
    out = capsys.readouterr().out
    record = history.list_records(parse_config(yaml.safe_load(cfg.read_text(encoding="utf-8")), cfg).project("erp"))[0]
    assert f"지금 반영된 패키지: {record.name}" in out and "apply" in out

    assert cli.main(["deploy", "-c", str(cfg), "-p", "erp", "--rollback"]) == 0
    assert "되돌렸습니다" in capsys.readouterr().out
    assert (server / "WEB-INF/views/user/list.jsp").read_text(encoding="utf-8") == "OLD"
    data = json.loads((record.path / history.RECORD_FILE).read_text(encoding="utf-8"))
    actions = [d["action"] for d in data["deployments"]]
    assert actions == ["check", "check", "apply", "rollback"] and all(d["ok"] for d in data["deployments"])  # 화면 기록과 같은 곳


@needs_bash
def test_deploy_password_from_keyring_and_missing(tmp_path, maven_project, sshd, monkeypatch, capsys):
    cfg, server, keyring = _setup(tmp_path, maven_project, sshd, monkeypatch)
    monkeypatch.delenv(remote_cli.ENV_LOGIN)
    assert cli.main(["package", "-c", str(cfg), "-p", "erp", "src/main/webapp/WEB-INF/views/user/list.jsp"]) == 0
    capsys.readouterr()
    assert cli.main(["deploy", "-c", str(cfg), "-p", "erp", "--accept-host-key"]) == 2   # 비밀번호가 아무 데도 없음
    assert remote_cli.ENV_LOGIN in capsys.readouterr().err
    keyring.store[("shipkit", f"ssh:login:deployer@127.0.0.1:{sshd.port}")] = LOGIN_PW   # 화면에서 저장한 것과 같은 키
    assert cli.main(["deploy", "-c", str(cfg), "-p", "erp", "--accept-host-key"]) == 0
    assert "확인만 했습니다" in capsys.readouterr().out
    keyring.store[("shipkit", f"ssh:login:deployer@127.0.0.1:{sshd.port}")] = "wrong"
    assert cli.main(["deploy", "-c", str(cfg), "-p", "erp"]) == 1
    assert "로그인에 실패" in capsys.readouterr().err
