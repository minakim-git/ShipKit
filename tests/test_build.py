"""프로젝트 빌드 (shipkit.build): 프리셋 기본 명령, 설정 build_command, 실행·출력·종료 코드·취소, 명령어 shipkit build."""
import sys
import threading
import time

import pytest
import yaml

from shipkit import build, cli
from shipkit.config import parse_config

PY = sys.executable.replace("\\", "/")


def _project(tmp_path, **extra):
    root = tmp_path / "proj"
    root.mkdir(exist_ok=True)
    data = {"projects": {"erp": dict({"root": root.as_posix(), "output_dir": (tmp_path / "out").as_posix(),
                                     "server_path": "/app"}, **extra)}}
    return parse_config(data).project("erp")


def test_default_and_configured_command(tmp_path):
    p = _project(tmp_path, preset="maven")
    assert build.command_for(p) == "mvn -DskipTests compile" and p.build_command == ""
    assert build.command_for(_project(tmp_path, preset="vite")) == "npm run build"
    assert build.command_for(_project(tmp_path, preset="static")) == ""            # 빌드가 필요 없는 프리셋
    assert build.command_for(_project(tmp_path, preset="maven", build_command="  mvn package  ")) == "mvn package"
    g = _project(tmp_path, preset="gradle")
    assert build.command_for(g) == "gradle classes"
    (g.root / ("gradlew.bat" if sys.platform == "win32" else "gradlew")).write_text("", encoding="utf-8")
    assert build.command_for(g).endswith("gradlew classes") or build.command_for(g) == "gradlew.bat classes"


def test_run_build_streams_output_and_exit_code(tmp_path):
    p = _project(tmp_path, preset="maven")
    lines = []
    cmd = f'"{PY}" -c "print(\'hello\'); print(\'\\ud55c\\uae00\'); import sys; sys.exit(3)"'
    r = build.run_build(p, lines.append, command=cmd)
    text = "".join(lines)
    assert r.code == 3 and not r.ok and not r.cancelled and r.command == cmd and r.summary().startswith("빌드 실패 (종료 코드 3")
    assert text.startswith(f"$ {cmd}\n") and "hello\n" in text and "한글" in text and str(p.root) in text
    ok = build.run_build(p, lines.append, command=f'"{PY}" -c "print(1)"')
    assert ok.ok and ok.summary().startswith("빌드 성공 (")
    with pytest.raises(build.BuildError, match="빌드 명령이 없습니다"):
        build.run_build(_project(tmp_path, preset="static"), lines.append)
    bad = _project(tmp_path, preset="maven")
    bad.root = tmp_path / "nowhere"
    with pytest.raises(build.BuildError, match="로컬 경로가 없습니다"):
        build.run_build(bad, lines.append, command="echo x")


def test_run_build_cancel(tmp_path):
    p = _project(tmp_path, preset="maven")
    lines, stop = [], [False]
    cmd = f'"{PY}" -c "import time,sys; print(\'start\', flush=True); time.sleep(30); print(\'late\')"'

    def cancel_soon():
        while not any("start" in ln for ln in lines):
            time.sleep(0.05)
        stop[0] = True

    threading.Thread(target=cancel_soon, daemon=True).start()
    t0 = time.monotonic()
    r = build.run_build(p, lines.append, is_cancelled=lambda: stop[0], command=cmd)
    assert r.cancelled and not r.ok and time.monotonic() - t0 < 15 and "[취소]" in "".join(lines)
    assert not any(ln.strip() == "late" for ln in lines)                           # 멈춘 뒤의 출력은 없다
    assert r.summary().startswith("빌드를 취소했습니다")


def test_cli_build(tmp_path, capsys):
    root = tmp_path / "proj"
    root.mkdir()
    cfg = tmp_path / "shipkit.yaml"
    cfg.write_text(yaml.safe_dump({"projects": {"erp": {"root": root.as_posix(), "output_dir": (tmp_path / "out").as_posix(),
                                                         "server_path": "/app", "preset": "maven",
                                                         "build_command": f'"{PY}" -c "print(\'built\')"'}}}), encoding="utf-8")
    assert cli.main(["build", "-c", str(cfg), "-p", "erp"]) == 0
    out = capsys.readouterr().out
    assert "built" in out and "빌드 성공" in out
    assert cli.main(["build", "-c", str(cfg), "-p", "erp", "--command", f'"{PY}" -c "raise SystemExit(2)"']) == 1
    assert "빌드 실패 (종료 코드 2" in capsys.readouterr().out


def test_find_tools_when_not_on_path(tmp_path, monkeypatch):
    """PATH 에 mvn·JDK 가 없는 PC(IDE 로만 빌드하던): 래퍼 → MAVEN_HOME → IntelliJ 의 Maven → 설치 폴더 순으로 찾고,
    JAVA_HOME 이 없으면 JDK 폴더를 찾아 환경에 넣는다. 못 찾으면 어떻게 하라고 알려 준다."""
    programs = tmp_path / "programs"
    monkeypatch.setattr(build, "_PROGRAM_DIRS", [str(programs)])
    monkeypatch.setattr(build.shutil, "which", lambda name: None)
    monkeypatch.delenv("JAVA_HOME", raising=False)
    monkeypatch.delenv("MAVEN_HOME", raising=False)
    monkeypatch.delenv("M2_HOME", raising=False)
    root = tmp_path / "proj"
    root.mkdir()
    assert build.find_tool("mvn", root) is None and build.find_java_home() is None
    with pytest.raises(build.BuildError, match="'mvn' 을 찾지 못했습니다.*IntelliJ"):
        build.resolve_command("mvn -DskipTests compile", root)
    assert build.resolve_command('"C:/x/mvn.cmd" compile', root) == ('"C:/x/mvn.cmd" compile', [])   # 전체 경로는 그대로
    assert build.resolve_command("echo hi", root) == ("echo hi", [])

    idea = programs / "JetBrains" / "IntelliJ IDEA 2025.3" / "plugins" / "maven" / "lib" / "maven3" / "bin"
    idea.mkdir(parents=True)
    mvn = idea / ("mvn.cmd" if sys.platform == "win32" else "mvn")
    mvn.write_text("", encoding="utf-8")
    assert build.find_tool("mvn", root) == mvn
    cmd, notes = build.resolve_command("mvn -DskipTests compile", root)
    assert cmd == f'"{mvn}" -DskipTests compile' and notes == [f"  (mvn 이 PATH 에 없어 {mvn} 를 씁니다)"]
    wrapper = root / ("mvnw.cmd" if sys.platform == "win32" else "mvnw")
    wrapper.write_text("", encoding="utf-8")
    assert build.find_tool("mvn", root) == wrapper                                   # 프로젝트 래퍼가 먼저
    home = tmp_path / "maven-home" / "bin"
    home.mkdir(parents=True)
    (home / mvn.name).write_text("", encoding="utf-8")
    wrapper.unlink()
    monkeypatch.setenv("MAVEN_HOME", str(home.parent))
    assert build.find_tool("mvn", root) == home / mvn.name                           # MAVEN_HOME 이 IntelliJ 보다 먼저

    jdk = programs / "Java" / "jdk-17" / "bin"
    jdk.mkdir(parents=True)
    (jdk / ("java.exe" if sys.platform == "win32" else "java")).write_text("", encoding="utf-8")
    assert build.find_java_home() == jdk.parent
    env, notes = build.build_env()
    assert env["JAVA_HOME"] == str(jdk.parent) and env["PATH"].startswith(str(jdk)) and notes == [f"  (JAVA_HOME 이 없어 {jdk.parent} 를 씁니다)"]
    monkeypatch.setenv("JAVA_HOME", str(jdk.parent))
    assert build.build_env()[1] == []                                                  # JAVA_HOME 이 있으면 그대로
