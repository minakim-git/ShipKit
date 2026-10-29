"""2단계: 반영 전·후 명령(hooks), 체크섬 검증, 동시 실행 잠금, 배포 이력, 새 파일 권한."""
import hashlib
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from shipkit import mapper, packager, script
from shipkit.config import ConfigError, ServerOptions, parse_config
from shipkit.hooks import select_hooks
from shipkit.models import ChangeType as C, FileChange, SourceInfo

from conftest import make_config, write
from test_package_e2e import BASH, needs_bash, run_deploy


def _cfg(**over):
    p = {"root": "D:/w/app", "output_dir": "D:/deploy", "server_path": "/srv/app"}
    p.update(over)
    return parse_config({"projects": {"app": p}}).project("app")


# ---------------------------------------------------------------------- 설정
def test_hooks_parse_forms():
    p = _cfg(hooks={
        "pre_apply": "sudo systemctl stop tomcat",
        "post_apply": ["sudo systemctl start tomcat", {"run": "pip install -r requirements.txt",
                                                       "when": "requirements*.txt"}],
    })
    assert [h.run for h in p.hooks["pre_apply"]] == ["sudo systemctl stop tomcat"]
    assert p.hooks["post_apply"][1].when == "requirements*.txt"


@pytest.mark.parametrize("hooks,msg", [
    ({"before": ["x"]}, "단계"),
    ({"post_apply": [""]}, "비어"),
    ({"post_apply": [{"when": "a"}]}, "비어"),
])
def test_hooks_invalid(hooks, msg):
    with pytest.raises(ConfigError, match=msg):
        _cfg(hooks=hooks)


def test_server_options():
    s = _cfg(server={"owner": "tomcat:tomcat", "file_mode": 644, "dir_mode": "0755"}).server
    assert (s.owner, s.file_mode, s.dir_mode, s.history_file) == ("tomcat:tomcat", "644", "0755", "~/.shipkit/history.log")
    assert _cfg(server={"owner": "inherit"}).server.owner == "inherit"
    assert _cfg().server == ServerOptions()


@pytest.mark.parametrize("server,msg", [
    ({"owner": "root; rm -rf /"}, "owner"),
    ({"file_mode": "999"}, "file_mode"),
    ({"dir_mode": "rwx"}, "dir_mode"),
    ({"history_file": "logs/h.log"}, "history_file"),
    ({"history_file": "/var/log/$USER.log"}, "history_file"),
])
def test_server_options_invalid(server, msg):
    with pytest.raises(ConfigError, match=msg):
        _cfg(server=server)


def test_select_hooks_when_filter():
    p = _cfg(hooks={"post_apply": ["restart", {"run": "pip install", "when": "requirements*.txt"}]})
    selected, notes = select_hooks(p, [FileChange("app/views.py", C.MODIFIED)])
    assert selected == {"post_apply": ["restart"]} and "pip install" in notes[0]
    selected, _ = select_hooks(p, [FileChange("requirements.txt", C.MODIFIED)])
    assert selected == {"post_apply": ["restart", "pip install"]}
    selected, notes = select_hooks(p, [])  # 변경 목록이 없으면(sync 전체 동기화) 조건부도 포함
    assert selected == {"post_apply": ["restart", "pip install"]} and "포함" in notes[0]


# ---------------------------------------------------------------------- 패키지
def build(maven_project, tmp_path, **over):
    project = make_config(maven_project, tmp_path / "out", **over).project("erp")
    changes = [FileChange("src/main/webapp/WEB-INF/views/user/list.jsp", C.MODIFIED, "!1"),
               FileChange("src/main/java/com/acme/User.java", C.MODIFIED, "!1")]
    entries = mapper.plan(project, changes)
    return packager.build_package(project, entries, SourceInfo("manual", "t"), source_changes=changes,
                                  now=datetime(2026, 10, 28, 9, 0))


def test_package_has_hooks_and_checksums(maven_project, tmp_path):
    result = build(maven_project, tmp_path, hooks={"post_apply": ["echo restarted"]},
                   server={"owner": "inherit", "file_mode": "640"})
    pkg = result.out_dir
    assert (pkg / "hooks/post_apply.sh").read_text(encoding="utf-8").rstrip().endswith("echo restarted")
    assert not (pkg / "hooks/pre_apply.sh").exists()
    lines = (pkg / "checksums.sha256").read_text(encoding="utf-8").splitlines()
    listed = {line.split("  ", 1)[1]: line.split("  ", 1)[0] for line in lines}
    assert "files/WEB-INF/views/user/list.jsp" in listed and "hooks/post_apply.sh" in listed
    assert "shipkit.sh" not in listed and "report.md" not in listed
    for rel, digest in listed.items():
        assert hashlib.sha256((pkg / rel).read_bytes()).hexdigest() == digest
    sh = (pkg / "shipkit.sh").read_text(encoding="utf-8")
    assert "NEW_OWNER='inherit'" in sh and "NEW_FILE_MODE='640'" in sh and "@@" not in sh
    assert "서버에서 실행할 명령" in (pkg / "report.md").read_text(encoding="utf-8")


def test_script_render_defaults():
    text = script.render("/srv/app", "pkg", datetime(2026, 1, 1))
    assert "NEW_OWNER=''" in text and "HISTORY_FILE='~/.shipkit/history.log'" in text


# ---------------------------------------------------------------------- 서버 스크립트 (bash 실제 실행)
def _server(tmp_path):
    server = tmp_path / "server"
    write(server / "WEB-INF/views/user/list.jsp", "OLD JSP")
    write(server / "WEB-INF/classes/com/acme/User.class", "OLD")
    return server


def _snapshot(server):
    return {f.relative_to(server).as_posix(): f.read_text(encoding="utf-8") for f in server.rglob("*") if f.is_file()}


@needs_bash
def test_checksum_mismatch_blocks(maven_project, tmp_path):
    pkg = build(maven_project, tmp_path).out_dir
    server = _server(tmp_path)
    (pkg / "files/WEB-INF/views/user/list.jsp").write_text("tampered", encoding="utf-8")
    code, out = run_deploy(pkg, "check", server.as_posix())
    assert code != 0 and "checksums.sha256 불일치" in out and "list.jsp" in out
    code, out = run_deploy(pkg, "backup", server.as_posix())
    assert code != 0 and not (pkg / "backup").exists()


def _translated_sha256sum(tmp_path):
    """한국어 로케일 서버처럼 'OK' 대신 '성공'을 출력하는 sha256sum. LC_ALL 을 무시하고 종료 코드는 그대로 둔다."""
    fake = tmp_path / "fake_sha256sum.sh"
    write(fake, 'sha256sum() { local out rc; out="$(command sha256sum "$@" 2>&1)"; rc=$?\n'
                '  printf "%s\\n" "$out" | sed "s/: OK$/: 성공/; s/: FAILED$/: 실패/"; return $rc; }\n')
    return {"BASH_ENV": fake.as_posix()}  # 비대화형 bash 가 스크립트 실행 전에 읽는다


@needs_bash
def test_checksum_ignores_translated_output(maven_project, tmp_path):
    pkg = build(maven_project, tmp_path).out_dir
    server = _server(tmp_path)
    env = _translated_sha256sum(tmp_path)
    code, out = run_deploy(pkg, "check", server.as_posix(), env=env)
    assert code == 0, out
    (pkg / "files/WEB-INF/views/user/list.jsp").write_text("tampered", encoding="utf-8")
    code, out = run_deploy(pkg, "check", server.as_posix(), env=env)
    assert code != 0 and "checksums.sha256 불일치" in out and "list.jsp" in out


@needs_bash
def test_hooks_need_confirmation_and_run_with_yes(maven_project, tmp_path):
    pkg = build(maven_project, tmp_path, hooks={
        "pre_apply": ["echo pre > pre_marker.txt"],
        "post_apply": ['echo "$DEPLOY_PACKAGE" > post_marker.txt'],
        "post_rollback": ["echo rolled > rollback_marker.txt"],
    }).out_dir
    server = _server(tmp_path)
    target = server.as_posix()
    before = _snapshot(server)

    code, out = run_deploy(pkg, "check", target)
    assert code == 0 and "pre_apply 명령" in out and "echo pre" in out

    assert run_deploy(pkg, "backup", target)[0] == 0
    # 터미널이 아니고 -y 도 없으면 pre_apply 를 실행하지 않고 apply 자체를 중단한다
    code, out = run_deploy(pkg, "apply", target)
    assert code != 0 and "apply 를 중단" in out
    assert _snapshot(server) == before

    code, out = run_deploy(pkg, "apply", target, "-y")
    assert code == 0, out
    after = _snapshot(server)
    assert after["pre_marker.txt"].strip() == "pre"
    assert after["post_marker.txt"].strip() == pkg.name
    assert after["WEB-INF/views/user/list.jsp"] != "OLD JSP"

    code, out = run_deploy(pkg, "rollback", "-y", target)  # 옵션 위치는 자유
    assert code == 0, out
    restored = _snapshot(server)
    assert restored["rollback_marker.txt"].strip() == "rolled"
    assert restored["WEB-INF/views/user/list.jsp"] == "OLD JSP"


@needs_bash
def test_pre_apply_failure_aborts_and_no_hooks_skips(maven_project, tmp_path):
    pkg = build(maven_project, tmp_path, hooks={"pre_apply": ["exit 3"]}).out_dir
    server = _server(tmp_path)
    target = server.as_posix()
    before = _snapshot(server)
    assert run_deploy(pkg, "backup", target)[0] == 0
    code, out = run_deploy(pkg, "apply", target, "-y")
    assert code != 0 and "pre_apply 명령이 실패" in out and "종료 코드 3" in out
    assert _snapshot(server) == before

    code, out = run_deploy(pkg, "apply", target, "--no-hooks")
    assert code == 0 and "--no-hooks" in out
    assert _snapshot(server)["WEB-INF/views/user/list.jsp"] != "OLD JSP"


@needs_bash
def test_history_and_options(maven_project, tmp_path):
    pkg = build(maven_project, tmp_path, server={"file_mode": "644", "dir_mode": "755"}).out_dir
    server = _server(tmp_path)
    target = server.as_posix()
    home = tmp_path / "home"
    code, out = run_deploy(pkg, "history", home=home)
    assert code == 0 and "배포 이력이 없습니다" in out
    assert run_deploy(pkg, "backup", target, home=home)[0] == 0
    assert run_deploy(pkg, "apply", target, home=home)[0] == 0
    code, out = run_deploy(pkg, "history", home=home)
    assert code == 0
    assert "backup" in out and "apply" in out and pkg.name in out and "반영 4개" in out
    assert (home / ".shipkit/history.log").is_file()
    code, out = run_deploy(pkg, "check", target, "--bogus", home=home)
    assert code != 0 and "알 수 없는 옵션" in out


@needs_bash
def test_lock_blocks_concurrent_run_and_clears_stale(maven_project, tmp_path):
    pkg = build(maven_project, tmp_path).out_dir
    server = _server(tmp_path)
    home = tmp_path / "lockhome"
    home.mkdir()
    lock_expr = ('key=$(printf "%s" "$(cd "$T" && pwd)" | cksum | cut -d" " -f1); '
                 'L="$TMPDIR/shipkit-$key.lock"; mkdir -p "$L"')
    script_path = (pkg / "shipkit.sh").as_posix()

    def run(body):
        env_cmd = (f'export HOME="{home.as_posix()}" TMPDIR="{home.as_posix()}"; '
                   f'T="{server.as_posix()}"; {lock_expr}; {body}')
        proc = subprocess.run([BASH, "-c", env_cmd], capture_output=True, stdin=subprocess.DEVNULL)
        return proc.returncode, proc.stdout.decode("utf-8", "replace") + proc.stderr.decode("utf-8", "replace")

    # 살아 있는 프로세스가 잡은 잠금 -> 막힘
    code, out = run(f'sleep 30 & echo $! > "$L/pid"; echo "someone apply" > "$L/info"; '
                    f'bash "{script_path}" backup "$T"; rc=$?; kill %1; rm -rf "$L"; exit $rc')
    assert code != 0 and "다른 배포가 진행 중" in out and "someone apply" in out
    assert not (pkg / "backup").exists()

    # 죽은 프로세스가 남긴 잠금 -> 정리하고 진행
    code, out = run(f'echo 999999 > "$L/pid"; bash "{script_path}" backup "$T"')
    assert code == 0 and "남긴 잠금을 정리" in out
    assert not any(p.name.endswith(".lock") for p in home.iterdir())  # 끝나면 잠금 해제
