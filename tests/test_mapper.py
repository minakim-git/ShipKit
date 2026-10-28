import os
import time

from shipkit import mapper
from shipkit.models import ChangeType as C, FileChange, Status

from conftest import make_config, write


def _plan(cfg, *changes):
    project = cfg.project("erp")
    return {e.change.path: e for e in mapper.plan(project, [FileChange(p, c) for p, c in changes])}


def targets(entry):
    return sorted(a.target for a in entry.artifacts)


def test_java_includes_only_own_classes(maven_project, tmp_path):
    cfg = make_config(maven_project, tmp_path / "out")
    e = _plan(cfg, ("src/main/java/com/acme/User.java", C.MODIFIED))["src/main/java/com/acme/User.java"]
    assert e.status == Status.OK, e.messages
    # UserService.class 는 들어가면 안 된다 (기존 툴의 User*.class 버그)
    assert targets(e) == [
        "WEB-INF/classes/com/acme/User$1.class",
        "WEB-INF/classes/com/acme/User$Inner.class",
        "WEB-INF/classes/com/acme/User.class",
    ]


def test_java_secondary_top_level_types(maven_project, tmp_path):
    cfg = make_config(maven_project, tmp_path / "out")
    e = _plan(cfg, ("src/main/java/com/acme/Multi.java", C.MODIFIED))["src/main/java/com/acme/Multi.java"]
    assert targets(e) == [
        "WEB-INF/classes/com/acme/Helper$1.class",
        "WEB-INF/classes/com/acme/Helper.class",
        "WEB-INF/classes/com/acme/Multi.class",
    ]


def test_java_stale_and_missing_class(maven_project, tmp_path):
    src = maven_project / "src/main/java/com/acme/UserService.java"
    os.utime(src, (time.time() + 60, time.time() + 60))
    write(maven_project / "src/main/java/com/acme/NotBuilt.java", "class NotBuilt {}")
    cfg = make_config(maven_project, tmp_path / "out")
    plan = _plan(cfg, ("src/main/java/com/acme/UserService.java", C.MODIFIED),
                 ("src/main/java/com/acme/NotBuilt.java", C.ADDED))
    assert plan["src/main/java/com/acme/UserService.java"].status == Status.WARN
    assert "빌드 누락" in plan["src/main/java/com/acme/UserService.java"].messages[0]
    assert plan["src/main/java/com/acme/NotBuilt.java"].status == Status.ERROR


def test_cp949_source(maven_project, tmp_path):
    path = maven_project / "src/main/java/com/acme/UserService.java"
    path.write_bytes("// 한글 주석\npublic class UserService {}\n".encode("cp949"))
    os.utime(path, (time.time() - 100, time.time() - 100))
    cfg = make_config(maven_project, tmp_path / "out")
    e = _plan(cfg, ("src/main/java/com/acme/UserService.java", C.MODIFIED))["src/main/java/com/acme/UserService.java"]
    assert e.status == Status.OK, e.messages


def test_resources_webapp_and_skips(maven_project, tmp_path):
    cfg = make_config(maven_project, tmp_path / "out")
    plan = _plan(cfg,
                 ("src/main/resources/mapper/user.xml", C.MODIFIED),
                 ("src/main/webapp/WEB-INF/views/user/list.jsp", C.ADDED),
                 ("pom.xml", C.MODIFIED),
                 ("README.md", C.MODIFIED),
                 ("src/main/webapp/missing.jsp", C.MODIFIED))
    assert targets(plan["src/main/resources/mapper/user.xml"]) == ["WEB-INF/classes/mapper/user.xml"]
    assert targets(plan["src/main/webapp/WEB-INF/views/user/list.jsp"]) == ["WEB-INF/views/user/list.jsp"]
    assert plan["pom.xml"].status == Status.SKIPPED
    assert plan["README.md"].status == Status.SKIPPED
    assert plan["src/main/webapp/missing.jsp"].status == Status.ERROR


def test_deletions(maven_project, tmp_path):
    cfg = make_config(maven_project, tmp_path / "out")
    plan = _plan(cfg,
                 ("src/main/webapp/old.jsp", C.DELETED),
                 ("src/main/java/com/acme/User.java", C.DELETED))
    assert plan["src/main/webapp/old.jsp"].deletes == ["old.jsp"]
    java = plan["src/main/java/com/acme/User.java"]
    assert java.deletes[0] == "WEB-INF/classes/com/acme/User.class"
    assert "WEB-INF/classes/com/acme/User$Inner.class" in java.deletes
    assert java.artifacts == []


def test_multi_module_and_custom_rules(tmp_path):
    root = tmp_path / "erp"
    write(root / "erp-web/src/main/webapp/a.jsp")
    write(root / "erp-web-admin/src/main/webapp/b.jsp")
    write(root / "erp-web/src/main/java/A.java", "class A {}", time.time() - 100)
    write(root / "erp-web/target/classes/A.class")
    cfg = make_config(root, tmp_path / "out", modules=["erp-web", "erp-web-admin"])
    plan = _plan(cfg, ("erp-web/src/main/webapp/a.jsp", C.MODIFIED),
                 ("erp-web-admin/src/main/webapp/b.jsp", C.MODIFIED),
                 ("erp-web/src/main/java/A.java", C.MODIFIED))
    assert targets(plan["erp-web/src/main/webapp/a.jsp"]) == ["a.jsp"]
    assert targets(plan["erp-web-admin/src/main/webapp/b.jsp"]) == ["b.jsp"]
    assert targets(plan["erp-web/src/main/java/A.java"]) == ["WEB-INF/classes/A.class"]


def test_resources_from_build_dir(maven_project, tmp_path):
    write(maven_project / "target/classes/config.properties", "filtered")
    write(maven_project / "src/main/resources/config.properties", "${raw}")
    write(maven_project / "src/main/resources/only-src.properties", "x")
    rules = [
        {"src": "src/main/resources", "dest": "WEB-INF/classes", "build_dir": "target/classes"},
    ]
    cfg = make_config(maven_project, tmp_path / "out", rules=rules)
    plan = _plan(cfg, ("src/main/resources/config.properties", C.MODIFIED),
                 ("src/main/resources/only-src.properties", C.MODIFIED))
    e = plan["src/main/resources/config.properties"]
    assert e.artifacts[0].local_file.read_text() == "filtered"
    assert plan["src/main/resources/only-src.properties"].status == Status.WARN
