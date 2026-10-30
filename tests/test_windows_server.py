"""5단계: Windows 서버(server_os: windows)용 shipkit.ps1 - 설정, 패키지, PowerShell 실제 실행."""
import hashlib
import os
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

import pytest

from shipkit import SCRIPT_NAME, SCRIPT_NAME_PS, mapper, packager, sync
from shipkit.config import ConfigError, parse_config
from shipkit.models import ChangeType as C, FileChange, SourceInfo

from conftest import make_config, write

POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")
needs_powershell = pytest.mark.skipif(POWERSHELL is None, reason="PowerShell 이 없습니다")


def _cfg(**over):
    p = {"root": "D:/w/app", "output_dir": "D:/deploy", "server_os": "windows", "server_path": "D:/tomcat/webapps/ROOT"}
    p.update(over)
    return parse_config({"projects": {"app": p}}).project("app")


# ---------------------------------------------------------------------- 설정
@pytest.mark.parametrize("raw,expected", [
    ("D:/tomcat/webapps/ROOT/", "D:\\tomcat\\webapps\\ROOT"),
    ("E:\\www\\site", "E:\\www\\site"),
    ("D:/", "D:\\"),
    ("\\\\nas\\share\\web", "\\\\nas\\share\\web"),
])
def test_windows_server_path(raw, expected):
    p = _cfg(server_path=raw)
    assert (p.server_path, p.server_os, p.script_name) == (expected, "windows", SCRIPT_NAME_PS)


@pytest.mark.parametrize("over,msg", [
    ({"server_path": "/opt/app"}, "Windows 절대 경로"),
    ({"server_path": "D:/a$b"}, "사용할 수 없는 문자"),
    ({"server": {"owner": "inherit"}}, "리눅스 서버에서만"),
    ({"server": {"history_file": "logs/h.log"}}, "history_file"),
    ({"server_os": "mac"}, "server_os"),
])
def test_windows_invalid(over, msg):
    with pytest.raises(ConfigError, match=msg):
        _cfg(**over)


def test_linux_default_unchanged():
    p = parse_config({"projects": {"a": {"root": "x", "output_dir": "y", "server_path": "/srv/a"}}}).project("a")
    assert (p.server_os, p.script_name) == ("linux", SCRIPT_NAME)


# ---------------------------------------------------------------------- 패키지
def build_windows(maven_project, tmp_path, **over):
    project = make_config(maven_project, tmp_path / "out", server_os="windows",
                          server_path="D:/tomcat/webapps/ROOT", **over).project("erp")
    changes = [
        FileChange("src/main/webapp/WEB-INF/views/user/list.jsp", C.MODIFIED, "!1"),
        FileChange("src/main/java/com/acme/User.java", C.MODIFIED, "!1"),
        FileChange("src/main/webapp/js/한글 파일.js", C.MODIFIED, "!1"),
        FileChange("src/main/webapp/old.jsp", C.DELETED, "!1"),
    ]
    entries = mapper.plan(project, changes)
    return packager.build_package(project, entries, SourceInfo("manual", "t"), source_changes=changes,
                                  now=datetime(2026, 10, 29, 9, 0))


def test_windows_package_layout(maven_project, tmp_path):
    result = build_windows(maven_project, tmp_path, hooks={"post_apply": ["Restart-Service Tomcat9"]})
    pkg = result.out_dir
    assert (pkg / SCRIPT_NAME_PS).is_file() and not (pkg / SCRIPT_NAME).exists()
    raw = (pkg / SCRIPT_NAME_PS).read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf") and b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")
    text = raw.decode("utf-8-sig")
    assert "$TargetDefault = 'D:\\tomcat\\webapps\\ROOT'" in text and "@@" not in text
    hook = (pkg / "hooks/post_apply.ps1").read_bytes().decode("utf-8-sig")
    assert "Restart-Service Tomcat9" in hook and "exit $LASTEXITCODE" in hook
    listed = (pkg / "checksums.sha256").read_text(encoding="utf-8")
    assert "hooks/post_apply.ps1" in listed and SCRIPT_NAME_PS not in listed
    report = (pkg / "report.md").read_text(encoding="utf-8")
    assert "powershell -ExecutionPolicy Bypass -File .\\shipkit.ps1 check" in report and "Unblock-File" in report


# ---------------------------------------------------------------------- PowerShell 실제 실행
def run_ps(pkg: Path, *args, sandbox: Path):
    """shipkit.ps1 실행. 출력은 UTF-8 로 받고, 이력·잠금이 실제 사용자 폴더에 남지 않도록 격리한다."""
    sandbox.mkdir(parents=True, exist_ok=True)
    quoted = " ".join(f"'{a}'" if not a.startswith("-") else a for a in args)
    command = ("[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
               f"& '{pkg / SCRIPT_NAME_PS}' {quoted}; exit $LASTEXITCODE")
    env = dict(os.environ, USERPROFILE=str(sandbox), TEMP=str(sandbox), TMP=str(sandbox))
    proc = subprocess.run([POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
                          capture_output=True, stdin=subprocess.DEVNULL, env=env)
    out = proc.stdout.decode("utf-8", "replace") + proc.stderr.decode("utf-8", "replace")
    return proc.returncode, out


def _server(tmp_path):
    server = tmp_path / "server root"
    write(server / "WEB-INF/views/user/list.jsp", "OLD JSP")
    write(server / "WEB-INF/classes/com/acme/User.class", "OLD")
    write(server / "old.jsp", "OLD JSP")
    write(server / "js/한글 파일.js", "// js", time.time() - 1000)  # 패키지와 같은 내용
    return server


def _snapshot(server):
    return {f.relative_to(server).as_posix(): f.read_bytes() for f in server.rglob("*") if f.is_file()}


@needs_powershell
def test_ps_full_cycle_with_hooks(maven_project, tmp_path):
    pkg = build_windows(maven_project, tmp_path, hooks={
        "pre_apply": ["Set-Content -Path pre_marker.txt -Value pre"],
        "post_apply": ["Set-Content -Path post_marker.txt -Value $env:DEPLOY_PACKAGE"],
        "post_rollback": ["Set-Content -Path rollback_marker.txt -Value rolled"],
    }).out_dir
    server = _server(tmp_path)
    target = str(server)
    sandbox = tmp_path / "sandbox"
    before = _snapshot(server)
    same_mtime = (server / "js/한글 파일.js").stat().st_mtime

    code, out = run_ps(pkg, "check", target, sandbox=sandbox)
    assert code == 0, out
    assert "[변경] WEB-INF/views/user/list.jsp" in out and "[동일] js/한글 파일.js" in out
    assert "[삭제예정] old.jsp" in out and "Set-Content -Path pre_marker.txt" in out

    code, out = run_ps(pkg, "apply", target, sandbox=sandbox)
    assert code != 0 and "백업이 없습니다" in out
    code, out = run_ps(pkg, "backup", target, sandbox=sandbox)
    assert code == 0, out
    code, out = run_ps(pkg, "backup", target, sandbox=sandbox)
    assert code != 0 and "이미 백업" in out

    code, out = run_ps(pkg, "apply", target, sandbox=sandbox)  # 터미널이 아니고 -Yes 도 없음 -> 중단
    assert code != 0 and "apply 를 중단" in out
    assert _snapshot(server) == before

    code, out = run_ps(pkg, "apply", target, "-Yes", sandbox=sandbox)
    assert code == 0, out
    assert "동일(건너뜀) 1개" in out and "삭제 1개" in out
    after = _snapshot(server)
    assert after["WEB-INF/views/user/list.jsp"] == b"<%-- jsp --%>"
    assert after["WEB-INF/classes/com/acme/User$Inner.class"] == b"bytecode"
    assert "old.jsp" not in after
    assert after["pre_marker.txt"].strip() == b"pre"
    assert after["post_marker.txt"].strip() == pkg.name.encode()
    assert (server / "js/한글 파일.js").stat().st_mtime == same_mtime  # 같은 파일은 건드리지 않음

    code, out = run_ps(pkg, "rollback", target, "-Yes", sandbox=sandbox)
    assert code == 0, out
    restored = _snapshot(server)
    assert restored.pop("rollback_marker.txt").strip() == b"rolled"
    restored.pop("pre_marker.txt"), restored.pop("post_marker.txt")
    assert restored == before

    code, out = run_ps(pkg, "history", sandbox=sandbox)
    assert code == 0 and "backup" in out and "apply" in out and "rollback" in out


@needs_powershell
def test_ps_rollback_removes_folders_apply_created(maven_project, tmp_path):
    """rollback 은 apply 가 새로 만든 폴더만 비어 있으면 지우고, 원래 있던 빈 폴더는 둔다."""
    pkg = build_windows(maven_project, tmp_path).out_dir
    server = tmp_path / "server root"
    write(server / "WEB-INF/classes/com/acme/User.class", "OLD")   # views 폴더는 없음 -> apply 가 만든다
    (server / "keep-empty").mkdir()
    sandbox = tmp_path / "sandbox"
    assert run_ps(pkg, "backup", str(server), sandbox=sandbox)[0] == 0
    code, out = run_ps(pkg, "apply", str(server), "-NoHooks", sandbox=sandbox)
    assert code == 0 and (server / "WEB-INF/views/user/list.jsp").is_file(), out
    code, out = run_ps(pkg, "rollback", str(server), "-NoHooks", sandbox=sandbox)
    assert code == 0 and "빈 폴더 정리 3개" in out, out              # WEB-INF/views/user, WEB-INF/views, js
    assert not (server / "WEB-INF/views").exists() and not (server / "js").exists() and (server / "keep-empty").is_dir()
    assert (server / "WEB-INF/classes/com/acme/User.class").read_text(encoding="utf-8") == "OLD"


@needs_powershell
def test_ps_checksum_and_pre_apply_failure(maven_project, tmp_path):
    pkg = build_windows(maven_project, tmp_path, hooks={"pre_apply": ["cmd /c exit 3"]}).out_dir
    server = _server(tmp_path)
    target, sandbox = str(server), tmp_path / "sandbox"
    before = _snapshot(server)
    assert run_ps(pkg, "backup", target, sandbox=sandbox)[0] == 0
    code, out = run_ps(pkg, "apply", target, "-Yes", sandbox=sandbox)
    assert code != 0 and "pre_apply 명령이 실패" in out and "종료 코드 3" in out
    assert _snapshot(server) == before
    code, out = run_ps(pkg, "apply", target, "-NoHooks", sandbox=sandbox)
    assert code == 0 and "-NoHooks" in out

    (pkg / "files/WEB-INF/views/user/list.jsp").write_text("tampered", encoding="utf-8")
    code, out = run_ps(pkg, "check", target, sandbox=sandbox)
    assert code != 0 and "checksums.sha256 불일치" in out and "list.jsp" in out


@needs_powershell
def test_ps_lock_blocks_live_process_and_clears_stale(maven_project, tmp_path):
    pkg = build_windows(maven_project, tmp_path).out_dir
    server = _server(tmp_path)
    sandbox = tmp_path / "sandbox"
    key = hashlib.sha256(str(server.resolve()).lower().encode("utf-8")).hexdigest()[:16]
    lock = sandbox / f"shipkit-{key}.lock"
    write(lock / "pid", str(os.getpid()))           # 살아 있는 프로세스(테스트 자신)가 잡은 잠금
    write(lock / "info", "someone apply")
    code, out = run_ps(pkg, "backup", str(server), sandbox=sandbox)
    assert code != 0 and "다른 배포가 진행 중" in out and "someone apply" in out
    (lock / "pid").write_text("999999", encoding="ascii")  # 죽은 프로세스가 남긴 잠금
    code, out = run_ps(pkg, "backup", str(server), sandbox=sandbox)
    assert code == 0 and "남긴 잠금을 정리" in out, out
    assert not lock.exists()


@needs_powershell
def test_ps_sync_prune(tmp_path):
    root = tmp_path / "web"
    write(root / "dist/index.html", "<script src=/assets/new.js>")
    write(root / "dist/assets/new.js", "new")
    p = make_config(root, tmp_path / "out", preset="vite", server_os="windows",
                    server_path="D:/www/web").project("erp")
    source = SourceInfo("build", "t")
    pkg = packager.build_package(p, sync.plan_sync(p, [], source), source).out_dir
    server = tmp_path / "www"
    write(server / "index.html", "<script src=/assets/old.js>")
    write(server / "assets/old.js", "old")
    write(server / "uploads/user.png", "keep")
    before = _snapshot(server)
    sandbox = tmp_path / "sandbox"
    code, out = run_ps(pkg, "check", str(server), sandbox=sandbox)
    assert code == 0 and "[정리예정] assets/old.js" in out and "uploads" not in out
    assert run_ps(pkg, "backup", str(server), sandbox=sandbox)[0] == 0
    code, out = run_ps(pkg, "apply", str(server), sandbox=sandbox)
    assert code == 0 and "정리 1개" in out, out
    after = _snapshot(server)
    assert "assets/old.js" not in after and after["uploads/user.png"] == b"keep"
    assert run_ps(pkg, "rollback", str(server), sandbox=sandbox)[0] == 0
    assert _snapshot(server) == before
