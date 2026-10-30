"""기록: 패키지마다 기록 파일을 남기고, 예전 패키지는 보고서에서 읽는다."""
import json
from datetime import datetime

from shipkit import history, packager, workflow
from shipkit.models import ChangeType, SourceInfo

from conftest import make_config, write


def _package(maven_project, out, lines, source=None, now=None):
    project = make_config(maven_project, out).projects["erp"]
    changes, src = workflow.collect_lines(lines, "직접 입력 (붙여넣기)")
    source = source or src
    entries = workflow.plan(project, changes, source)
    return project, packager.build_package(project, entries, source, source_changes=changes, now=now)


def test_deployments_appended_and_summarized(maven_project, tmp_path):
    """화면에서 서버에 올린 결과는 기록 파일에 쌓이고, 서버마다 마지막 상태와 '지금 반영된 서버'를 알 수 있다."""
    _, result = _package(maven_project, tmp_path / "out", ["src/main/webapp/WEB-INF/views/user/list.jsp"],
                         now=datetime(2026, 10, 1, 9, 0))
    folder = result.out_dir
    when = datetime(2026, 10, 1, 10, 0)
    history.append_deployment(folder, "개발", "deployer@10.0.0.5", "check", 0, "/home/d/deploy/p", "/srv/app", when)
    history.append_deployment(folder, "개발", "deployer@10.0.0.5", "apply", 0, "/home/d/deploy/p", "/srv/app", when)
    history.append_deployment(folder, "운영", "app@prod", "apply", 1, "/home/app/deploy/p", "/srv/app", when)
    r = history.read_record(folder)
    assert [d["action"] for d in r.deployments] == ["check", "apply", "apply"]
    assert r.deployments[0]["at"] == "2026-10-01T10:00:00" and r.deployments[2]["ok"] is False
    states = r.server_states()
    assert set(states) == {"개발", "운영"} and states["개발"]["action"] == "apply" and states["개발"]["ok"]
    assert [d["server"] for d in r.applied_servers()] == ["개발"]       # 운영은 실패했으니 반영된 서버가 아님
    history.append_deployment(folder, "개발", "deployer@10.0.0.5", "rollback", 0, "/home/d/deploy/p", "/srv/app")
    r = history.read_record(folder)
    assert r.applied_servers() == [] and r.server_states()["개발"]["action"] == "rollback"
    assert "개발" in r.search_text()
    data = json.loads((folder / history.RECORD_FILE).read_text(encoding="utf-8"))
    assert data["project"] == "erp" and len(data["deployments"]) == 4   # 다른 내용은 그대로


def test_record_written_and_listed(maven_project, tmp_path):
    out = tmp_path / "out"
    _, first = _package(maven_project, out, ["src/main/webapp/WEB-INF/views/user/list.jsp"],
                        now=datetime(2026, 10, 1, 9, 0))
    mr = {"state": "merged", "merged_at": "", "source_branch": "f", "target_branch": "main", "file_count": 1}
    source = SourceInfo(kind="gitlab-mr", description="GitLab MR !12, !15", merge_requests=[
        dict(mr, iid=12, ref="!12", title="사용자 목록", author="kim", web_url="https://g/12", commit_sha="a"),
        dict(mr, iid=15, ref="!15", title="정렬", author="lee", web_url="https://g/15", commit_sha="b")])
    project, second = _package(maven_project, out, ["src/main/java/com/acme/User.java", "D\tsrc/main/webapp/old.jsp",
                                                    "src/main/webapp/nope.jsp"], source=source,
                               now=datetime(2026, 10, 2, 9, 0))

    data = json.loads((second.out_dir / history.RECORD_FILE).read_text(encoding="utf-8"))
    assert data["counts"]["error"] == 1 and data["source"]["requests"][1]["ref"] == "!15"
    assert history.RECORD_FILE not in (second.out_dir / "checksums.sha256").read_text(encoding="utf-8")

    records = history.list_records(project)
    assert [r.name for r in records] == [second.out_dir.name, first.out_dir.name]  # 최근 것부터
    r = records[0]
    assert r.request_kind == "gitlab" and r.counts["error"] == 1 and r.has_record
    assert "WEB-INF/classes/com/acme/User.class" in r.applied and r.deletes == ["old.jsp"]
    assert "M\tsrc/main/java/com/acme/User.java" in r.changes and "D\tsrc/main/webapp/old.jsp" in r.changes
    assert "user.class" in r.search_text() and "정렬" in r.search_text()

    # 기록의 변경 목록을 그대로 붙여넣으면 같은 변경이 된다 (다시 만들기)
    again, _ = workflow.collect_lines(r.changes, "다시 만들기")
    assert {(c.path, c.change) for c in again} >= {("src/main/webapp/old.jsp", ChangeType.DELETED)}


def test_old_package_without_record(tmp_path):
    folder = tmp_path / "out" / "2026-09-30-1700"
    write(folder / "manifest.txt", "WEB-INF/a.jsp\nWEB-INF/b.jsp\n")
    write(folder / "delete.txt", "WEB-INF/old.jsp\n")
    write(folder / "shipkit.sh", "#!/bin/bash")
    write(folder / "report.md", "# 배포 패키지 보고서 - erp\n\n- **생성 시각**: 2026-09-30 17:00:05\n"
                                "- **서버 배포 경로**: `/app/ROOT`\n- **변경 출처**: GitLab MR !7, !9\n")
    write(tmp_path / "out" / "not-a-package" / "readme.txt", "x")
    r = history.read_record(folder)
    assert not r.has_record and r.created_at == datetime(2026, 9, 30, 17, 0, 5)
    assert r.server_path == "/app/ROOT" and r.source_kind == "gitlab-mr"
    assert [q["iid"] for q in r.requests] == [7, 9] and r.deletes == ["WEB-INF/old.jsp"]
    assert history.read_record(tmp_path / "out" / "not-a-package") is None
