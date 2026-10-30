"""패키지 생성 -> 서버(임시 폴더)에서 shipkit.sh check/backup/apply/rollback 까지 실제 실행."""
import os
import shutil
import subprocess
import tarfile
from datetime import datetime
from pathlib import Path

import pytest
import yaml

from shipkit import SCRIPT_NAME, cli, mapper, packager
from shipkit.models import ChangeType as C, FileChange, SourceInfo

from conftest import make_config, write


def find_bash():
    env = os.environ.get("SHIPKIT_TEST_BASH")
    if env:
        return env
    git_bash = Path(r"C:\Program Files\Git\bin\bash.exe")
    if os.name == "nt" and git_bash.exists():
        return str(git_bash)  # System32\bash.exe(WSL) 대신 Git Bash 사용
    return shutil.which("bash")


BASH = find_bash()
needs_bash = pytest.mark.skipif(BASH is None, reason="bash 가 없습니다")

CHANGES = [
    FileChange("src/main/java/com/acme/User.java", C.MODIFIED, "!1"),
    FileChange("src/main/webapp/WEB-INF/views/user/list.jsp", C.ADDED, "!1"),
    FileChange("src/main/webapp/js/한글 파일.js", C.MODIFIED, "!1"),
    FileChange("src/main/webapp/old.jsp", C.DELETED, "!1"),
    FileChange("pom.xml", C.MODIFIED, "!1"),
]


def build(maven_project, tmp_path, archive=None):
    project = make_config(maven_project, tmp_path / "out").project("erp")
    entries = mapper.plan(project, list(CHANGES))
    source = SourceInfo("manual", "테스트")
    return packager.build_package(project, entries, source, archive=archive, now=datetime(2026, 10, 27, 9, 0, 0))


def test_package_layout(maven_project, tmp_path):
    result = build(maven_project, tmp_path, archive="tar")
    out = result.out_dir
    assert out.name == "2026-10-27-0900"  # 기존 툴과 같은 날짜 폴더 이름
    assert (out / "files/WEB-INF/classes/com/acme/User.class").is_file()
    assert not (out / "files/WEB-INF/classes/com/acme/UserService.class").exists()
    assert (out / "files/js/한글 파일.js").is_file()
    assert (out / "delete.txt").read_text(encoding="utf-8") == "old.jsp\n"
    for name in ("shipkit.sh", "manifest.txt", "delete.txt", "report.md"):
        assert b"\r" not in (out / name).read_bytes(), f"{name} 에 CR 이 있음"
    report = (out / "report.md").read_text(encoding="utf-8")
    assert "UserService" not in report and "매핑 규칙 없음" in report

    with tarfile.open(result.archive) as tar:
        member = tar.getmember(f"{out.name}/shipkit.sh")
        assert member.mode == 0o755

    # 같은 초에 다시 만들어도 덮어쓰지 않는다
    again = build(maven_project, tmp_path)
    assert again.out_dir.name == "2026-10-27-0900_2"


def run_deploy(pkg: Path, *args, home: Path = None, env: dict = None):
    """서버 스크립트 실행. 배포 이력·잠금이 실제 홈/임시 폴더에 남지 않도록 HOME·TMPDIR 을 격리한다."""
    sandbox = home or (pkg.parent / "_home")
    sandbox.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, HOME=sandbox.as_posix(), TMPDIR=sandbox.as_posix(), **(env or {}))
    proc = subprocess.run([BASH, (pkg / SCRIPT_NAME).as_posix(), *args], capture_output=True,
                          stdin=subprocess.DEVNULL, env=env)
    return proc.returncode, proc.stdout.decode("utf-8", "replace") + proc.stderr.decode("utf-8", "replace")


@needs_bash
def test_deploy_script_backup_apply_rollback(maven_project, tmp_path):
    result = build(maven_project, tmp_path)
    pkg = result.out_dir
    server = tmp_path / "server root"
    write(server / "WEB-INF/classes/com/acme/User.class", "OLD")
    write(server / "WEB-INF/classes/com/acme/UserService.class", "UNTOUCHED")
    write(server / "old.jsp", "OLD JSP")
    write(server / "js/한글 파일.js", "OLD JS")
    target = server.as_posix()

    def snapshot():
        return {p.relative_to(server).as_posix(): p.read_text(encoding="utf-8")
                for p in server.rglob("*") if p.is_file()}

    before = snapshot()

    code, out = run_deploy(pkg, "apply", target)
    assert code != 0 and "백업이 없습니다" in out

    code, out = run_deploy(pkg, "check", target)
    assert code == 0, out
    assert "[신규] WEB-INF/views/user/list.jsp" in out and "[삭제예정] old.jsp" in out

    code, out = run_deploy(pkg, "backup", target)
    assert code == 0, out
    code, out = run_deploy(pkg, "backup", target)
    assert code != 0 and "이미 백업" in out

    code, out = run_deploy(pkg, "apply", target)
    assert code == 0, out
    after = snapshot()
    assert after["WEB-INF/classes/com/acme/User.class"] == "bytecode"
    assert after["WEB-INF/classes/com/acme/User$Inner.class"] == "bytecode"
    assert after["WEB-INF/views/user/list.jsp"] == "<%-- jsp --%>"
    assert after["js/한글 파일.js"] == "// js"
    assert after["WEB-INF/classes/com/acme/UserService.class"] == "UNTOUCHED"
    assert "old.jsp" not in after

    assert (server / "WEB-INF/views/user").is_dir()              # apply 가 새로 만든 폴더
    (server / "keep-empty").mkdir()                                # 원래 있던 빈 폴더는 rollback 이 건드리면 안 됨

    code, out = run_deploy(pkg, "rollback", target)
    assert code == 0, out
    assert snapshot() == before
    assert "빈 폴더 정리 2개" in out                                # WEB-INF/views/user, WEB-INF/views
    assert not (server / "WEB-INF/views").exists() and (server / "keep-empty").is_dir()


@needs_bash
def test_deploy_script_rejects_missing_target(maven_project, tmp_path):
    result = build(maven_project, tmp_path)
    code, out = run_deploy(result.out_dir, "check", (tmp_path / "nope").as_posix())
    assert code != 0 and "대상 경로가 없습니다" in out


def _write_config(path: Path, maven_project: Path, out: Path) -> Path:
    data = {"projects": {"erp": {
        "root": maven_project.as_posix(), "output_dir": out.as_posix(),
        "server_path": "/app/ROOT", "exclude": ["**/*.md"],
    }}}
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return path


def test_cli_package_from_file_list(maven_project, tmp_path, capsys):
    cfg = _write_config(tmp_path / "shipkit.yaml", maven_project, tmp_path / "out")
    listing = tmp_path / "list.txt"
    listing.write_text("src/main/java/com/acme/User.java\nsrc\\main\\webapp\\WEB-INF\\views\\user\\list.jsp\n",
                       encoding="utf-8")
    code = cli.main(["package", "-c", str(cfg), "--files", str(listing)])
    assert code == 0, capsys.readouterr().out
    pkgs = list((tmp_path / "out").iterdir())
    assert len(pkgs) == 1 and (pkgs[0] / "shipkit.sh").is_file()


def test_cli_blocks_on_errors_unless_forced(maven_project, tmp_path, capsys):
    cfg = _write_config(tmp_path / "shipkit.yaml", maven_project, tmp_path / "out")
    args = ["package", "-c", str(cfg), "src/main/webapp/WEB-INF/views/user/list.jsp", "src/main/webapp/nope.jsp"]
    assert cli.main(args) == 1
    assert "패키지를 만들지 않았습니다" in capsys.readouterr().out
    assert not (tmp_path / "out").exists()
    assert cli.main(args + ["--force"]) == 1  # 만들되 실패가 있었음을 종료코드로 알린다
    assert len(list((tmp_path / "out").iterdir())) == 1


def test_cli_usage_errors(maven_project, tmp_path, capsys):
    cfg = _write_config(tmp_path / "shipkit.yaml", maven_project, tmp_path / "out")
    assert cli.main(["package", "-c", str(cfg)]) == 2
    assert cli.main(["package", "-c", str(tmp_path / "none.yaml"), "a.jsp"]) == 2
    assert cli.main(["mr", "-c", str(cfg)]) == 2  # gitlab 섹션 없음


@pytest.mark.skipif(shutil.which("git") is None, reason="git 이 없습니다")
def test_cli_package_from_git_range(maven_project, tmp_path, capsys):
    def git(*args):
        subprocess.run(["git", "-C", str(maven_project), "-c", "user.name=t", "-c", "user.email=t@t",
                        *args], check=True, capture_output=True)

    git("init", "-q")
    git("add", "-A")
    git("commit", "-q", "-m", "init")
    git("tag", "v1")
    write(maven_project / "src/main/webapp/WEB-INF/views/user/list.jsp", "changed")
    git("mv", "src/main/resources/mapper/user.xml", "src/main/resources/mapper/member.xml")
    git("commit", "-q", "-am", "change")

    cfg = _write_config(tmp_path / "shipkit.yaml", maven_project, tmp_path / "out")
    code = cli.main(["package", "-c", str(cfg), "--git", "v1..HEAD"])
    out = capsys.readouterr().out
    assert code == 0, out
    pkg = next((tmp_path / "out").iterdir())
    assert (pkg / "manifest.txt").read_text(encoding="utf-8").splitlines() == [
        "WEB-INF/classes/mapper/member.xml", "WEB-INF/views/user/list.jsp",
    ]
    assert (pkg / "delete.txt").read_text(encoding="utf-8") == "WEB-INF/classes/mapper/user.xml\n"
