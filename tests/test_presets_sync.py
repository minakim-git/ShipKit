"""1단계: 프리셋, 민감 파일 차단, 알림, 빌드 결과 동기화(sync)와 서버의 옛 파일 정리."""
import os
import time
from datetime import datetime
from pathlib import Path

import pytest
import yaml

from shipkit import SCRIPT_NAME, cli, mapper, packager, script, sync
from shipkit.config import PRESETS, ConfigError, parse_config
from shipkit.models import ChangeType as C, FileChange, SourceInfo, Status

from conftest import write
from test_package_e2e import needs_bash, run_deploy


def _cfg(**over):
    p = {"root": "D:/w/app", "output_dir": "D:/deploy", "server_path": "/srv/app"}
    p.update(over)
    return parse_config({"projects": {"app": p}}).project("app")


# ---------------------------------------------------------------------- 설정
@pytest.mark.parametrize("name", list(PRESETS))
def test_every_preset_parses(name):
    p = _cfg(preset=name)
    assert p.mode == PRESETS[name]["mode"]
    assert p.preset == name


def test_aliases_and_modes():
    assert _cfg(preset="vue-vite").preset == "vite"
    p = _cfg(preset="vite")
    assert (p.mode, p.build_dir, p.prune, p.rules) == ("sync", "dist", ["assets"], [])
    assert "빌드 결과 동기화" in p.describe_mode
    assert _cfg(build_dir="out").mode == "sync"  # 프리셋 없이 build_dir 만 주면 sync
    assert _cfg(rules=[{"src": "", "dest": "html"}]).preset is None


@pytest.mark.parametrize("over,msg", [
    ({"preset": "nope"}, "preset"),
    ({"mode": "sync"}, "build_dir"),
    ({"preset": "vite", "prune": [""]}, "prune"),
    ({"preset": "vite", "prune": ["../etc"]}, "prune"),
    ({"preset": "vite", "prune": ["/etc"]}, "prune"),
    ({"preset": "vite", "build_dir": "../dist"}, "build_dir"),
    ({"mode": "fast"}, "mode"),
])
def test_invalid_settings(over, msg):
    with pytest.raises(ConfigError, match=msg):
        _cfg(**over)


def test_user_exclude_and_notices_extend_preset():
    p = _cfg(preset="python", exclude=["docs/**"], notices=[{"pattern": "deploy/*", "message": "배포 파일"}])
    assert p.is_excluded("app/__pycache__/x.pyc") and p.is_excluded("docs/a.txt") and p.is_excluded("README.md")
    assert p.notices_for("requirements.txt") and p.notices_for("deploy/run.sh") == ["배포 파일"]


# ---------------------------------------------------------------------- changes 모드: python/static
def test_python_preset_mapping_sensitive_and_notices(tmp_path):
    root = tmp_path / "api"
    write(root / "app/views.py", "def v(): pass")
    write(root / "app/__pycache__/views.cpython-312.pyc", "x")
    write(root / ".env", "SECRET=1")
    write(root / ".env.example", "SECRET=")
    write(root / "requirements.txt", "django")
    write(root / "app/migrations/0002_add.py", "# m")
    p = _cfg(preset="python", root=str(root))
    changes = [FileChange(path, c) for path, c in [
        ("app/views.py", C.MODIFIED), ("app/__pycache__/views.cpython-312.pyc", C.MODIFIED),
        (".env", C.MODIFIED), (".env.example", C.MODIFIED), ("requirements.txt", C.MODIFIED),
        ("app/migrations/0002_add.py", C.ADDED), ("config/.env.production", C.DELETED),
    ]]
    plan = {e.change.path: e for e in mapper.plan(p, changes)}
    assert [a.target for a in plan["app/views.py"].artifacts] == ["app/views.py"]
    assert plan["app/__pycache__/views.cpython-312.pyc"].status == Status.SKIPPED
    assert plan[".env"].status == Status.ERROR and "민감 파일" in plan[".env"].messages[0]
    assert plan[".env.example"].status == Status.OK
    assert plan["config/.env.production"].status == Status.SKIPPED  # 서버의 민감 파일은 지우지 않는다
    notes = mapper.collect_notices(p, changes)
    assert any("pip install" in n for n in notes) and any("migrate" in n for n in notes)


def test_allow_overrides_deny(tmp_path):
    root = tmp_path / "site"
    write(root / "certs/public.pem", "x")
    p = _cfg(preset="static", root=str(root), allow=["certs/public.pem"])
    entry = mapper.plan_one(p, FileChange("certs/public.pem", C.MODIFIED))
    assert entry.status == Status.OK


def test_maven_pom_notice_even_when_not_deployed(maven_project):
    p = _cfg(preset="maven", root=str(maven_project))
    changes = [FileChange("pom.xml", C.MODIFIED)]
    assert mapper.plan(p, changes)[0].status == Status.SKIPPED
    assert any("WEB-INF/lib" in n for n in mapper.collect_notices(p, changes))


# ---------------------------------------------------------------------- sync 모드
@pytest.fixture
def vite_project(tmp_path: Path) -> Path:
    root = tmp_path / "web"
    old = time.time() - 100
    write(root / "src/main.js", "console.log(1)", old)
    write(root / "package.json", "{}", old)
    write(root / "dist/index.html", "<script src=/assets/index-new.js>")
    write(root / "dist/assets/index-new.js", "new bundle")
    write(root / "dist/assets/logo.svg", "<svg/>")
    write(root / "dist/favicon.ico", "ico")
    write(root / "dist/README.md", "excluded")
    return root


def test_plan_sync(vite_project):
    p = _cfg(preset="vite", root=str(vite_project), dest="admin")
    source = SourceInfo("manual", "t")
    entries = sync.plan_sync(p, [], source)
    targets = sorted(a.target for e in entries for a in e.artifacts)
    assert targets == ["admin/assets/index-new.js", "admin/assets/logo.svg", "admin/favicon.ico", "admin/index.html"]
    assert source.warnings == []


def test_plan_sync_warnings(vite_project, tmp_path):
    src = vite_project / "src/main.js"
    os.utime(src, (time.time() + 60, time.time() + 60))
    write(vite_project / "dist/.env", "LEAK")
    p = _cfg(preset="vite", root=str(vite_project))
    source = SourceInfo("manual", "t")
    entries = sync.plan_sync(p, [FileChange("src/main.js", C.MODIFIED)], source)
    assert any("빌드 누락" in w for w in source.warnings)
    assert any(e.status == Status.ERROR and e.change.path == "dist/.env" for e in entries)

    empty = _cfg(preset="vite", root=str(tmp_path / "nothing"))
    source = SourceInfo("manual", "t")
    assert sync.plan_sync(empty, [], source) == [] and "빌드 결과 폴더가 없습니다" in source.warnings[0]


def _write_config(path: Path, root: Path, out: Path, **extra) -> Path:
    project = {"root": root.as_posix(), "output_dir": out.as_posix(), "server_path": "/var/www/web"}
    project.update(extra)
    path.write_text(yaml.safe_dump({"projects": {"web": project}}, allow_unicode=True), encoding="utf-8")
    return path


def test_cli_sync_package_without_source(vite_project, tmp_path, capsys):
    cfg = _write_config(tmp_path / "c.yaml", vite_project, tmp_path / "out", preset="vite")
    assert cli.main(["package", "-c", str(cfg)]) == 0, capsys.readouterr().out
    out = capsys.readouterr().out
    assert "빌드 결과 4개 파일" in out
    pkg = next((tmp_path / "out").iterdir())
    assert (pkg / "prune.txt").read_text(encoding="utf-8") == "assets\n"
    assert (pkg / "files/assets/index-new.js").is_file()
    report = (pkg / "report.md").read_text(encoding="utf-8")
    assert "빌드 결과 동기화" in report and "옛 빌드 파일 정리" in report


def test_cli_changes_mode_still_requires_source(maven_project, tmp_path):
    cfg = _write_config(tmp_path / "c.yaml", maven_project, tmp_path / "out", preset="maven")
    assert cli.main(["package", "-c", str(cfg)]) == 2


def test_cli_sync_with_notices(vite_project, tmp_path, capsys):
    cfg = _write_config(tmp_path / "c.yaml", vite_project, tmp_path / "out", preset="vite")
    assert cli.main(["package", "-c", str(cfg), "package.json", "src/main.js", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "[알림] package.json 변경" in out


def test_presets_command(capsys):
    assert cli.main(["presets"]) == 0
    out = capsys.readouterr().out
    assert all(name in out for name in PRESETS)


def test_script_render_has_no_placeholders():
    text = script.render("/srv/app", "pkg", datetime(2026, 1, 1))
    assert "@@" not in text and f"./{SCRIPT_NAME} check" in text


@needs_bash
def test_sync_deploy_prunes_and_rolls_back(vite_project, tmp_path):
    p = _cfg(preset="vite", root=str(vite_project), output_dir=str(tmp_path / "out"))
    source = SourceInfo("build", "t")
    result = packager.build_package(p, sync.plan_sync(p, [], source), source)
    pkg = result.out_dir

    server = tmp_path / "www"
    write(server / "index.html", "<script src=/assets/index-old.js>")
    write(server / "assets/index-old.js", "old bundle")
    write(server / "assets/logo.svg", "<svg/>", time.time() - 1000)  # 빌드 결과와 같은 파일
    write(server / "uploads/user.png", "user data")                  # 정리 폴더 밖: 건드리면 안 됨
    target = server.as_posix()

    def snapshot():
        return {f.relative_to(server).as_posix(): f.read_text(encoding="utf-8")
                for f in server.rglob("*") if f.is_file()}

    before = snapshot()
    logo_mtime = (server / "assets/logo.svg").stat().st_mtime

    code, out = run_deploy(pkg, "check", target)
    assert code == 0, out
    assert "[정리예정] assets/index-old.js" in out and "uploads" not in out
    assert "[동일] assets/logo.svg" in out

    assert run_deploy(pkg, "backup", target)[0] == 0
    code, out = run_deploy(pkg, "apply", target)
    assert code == 0, out
    assert "동일(건너뜀) 1개" in out and "정리 1개" in out
    after = snapshot()
    assert "assets/index-old.js" not in after
    assert after["assets/index-new.js"] == "new bundle" and after["uploads/user.png"] == "user data"
    assert (server / "assets/logo.svg").stat().st_mtime == logo_mtime  # 같은 파일은 건드리지 않음

    code, out = run_deploy(pkg, "rollback", target)
    assert code == 0, out
    assert snapshot() == before
