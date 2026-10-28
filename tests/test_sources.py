import pytest

from shipkit.config import GitLabConfig
from shipkit.models import ChangeType as C
from shipkit.sources import manual
from shipkit.sources.git_local import GitError, parse_name_status_z, parse_range
from shipkit.sources.gitlab import GitLabClient, GitLabError, collect_from_mrs

from conftest import FakeResponse, FakeSession

API = "https://git.acme.local/api/v4"
PID = "group%2Ferp"


def test_manual_parse_plain_and_name_status():
    text = """
    # 주석
    src/main/java/a/B.java
    D\tsrc/main/webapp/old.jsp
    A  src/main/webapp/new.jsp
    R087\tsrc/main/webapp/a.jsp\tsrc/main/webapp/b.jsp
    """
    changes, errors = manual.parse_lines(text.splitlines())
    assert errors == []
    assert [(c.change, c.path) for c in changes] == [
        (C.MODIFIED, "src/main/java/a/B.java"),
        (C.DELETED, "src/main/webapp/old.jsp"),
        (C.ADDED, "src/main/webapp/new.jsp"),
        (C.DELETED, "src/main/webapp/a.jsp"),
        (C.ADDED, "src/main/webapp/b.jsp"),
    ]


def test_manual_parse_reports_bad_lines():
    _, errors = manual.parse_lines(["/etc/passwd", "ok/file.jsp", "../x"])
    assert len(errors) == 2 and errors[0].startswith("1행")


def test_git_name_status_z():
    out = "M\0src/a.java\0A\0src/new.jsp\0D\0src/old.jsp\0R100\0src/r1.xml\0src/r2.xml\0"
    changes = parse_name_status_z(out, "git")
    assert [(c.change, c.path) for c in changes] == [
        (C.MODIFIED, "src/a.java"), (C.ADDED, "src/new.jsp"), (C.DELETED, "src/old.jsp"),
        (C.DELETED, "src/r1.xml"), (C.ADDED, "src/r2.xml"),
    ]


def test_parse_range():
    assert parse_range("v1..v2") == ("v1", "v2")
    assert parse_range("v1") == ("v1", "HEAD")
    with pytest.raises(GitError):
        parse_range("a...b")


def _client(routes, **cfg):
    session = FakeSession(routes)
    return GitLabClient(GitLabConfig(url="https://git.acme.local/", **cfg), "tok", session), session


def _mr(iid, merged_at, state="merged"):
    return FakeResponse(200, {
        "iid": iid, "title": f"MR {iid}", "author": {"name": "홍길동"}, "state": state,
        "merged_at": merged_at, "web_url": f"https://git/mr/{iid}", "merge_commit_sha": f"sha{iid}",
        "source_branch": f"feature/{iid}", "target_branch": "main",
    })


def test_gitlab_diffs_paging_and_merge_order():
    base = f"{API}/projects/{PID}/merge_requests"
    routes = {
        f"{base}/7": _mr(7, "2026-10-02T10:00:00Z"),
        f"{base}/5": _mr(5, "2026-10-01T10:00:00Z"),
        f"{base}/5/diffs": [
            FakeResponse(200, [{"old_path": "a.jsp", "new_path": "a.jsp", "new_file": True}], {"X-Next-Page": "2"}),
            FakeResponse(200, [{"old_path": "b.jsp", "new_path": "b.jsp"}], {"X-Next-Page": ""}),
        ],
        f"{base}/7/diffs": [FakeResponse(200, [
            {"old_path": "a.jsp", "new_path": "a.jsp", "deleted_file": True},
            {"old_path": "c.jsp", "new_path": "d.jsp", "renamed_file": True},
        ])],
    }
    client, session = _client(routes)
    changes, source = collect_from_mrs(client, "group/erp", [7, 5])
    assert session.headers["PRIVATE-TOKEN"] == "tok"
    assert all(call[2] == 15.0 and call[3] is True for call in session.calls)  # timeout, SSL 검증
    # !5 가 먼저 머지 -> a.jsp 추가 후 !7 에서 삭제되어 상쇄
    assert [(c.change, c.path) for c in changes] == [
        (C.MODIFIED, "b.jsp"), (C.DELETED, "c.jsp"), (C.ADDED, "d.jsp"),
    ]
    assert [m["iid"] for m in source.merge_requests] == [5, 7]
    assert source.merge_requests[0]["commit_sha"] == "sha5"
    assert source.warnings == []


def test_gitlab_falls_back_to_changes_and_warns_overflow():
    base = f"{API}/projects/{PID}/merge_requests"
    routes = {
        f"{base}/3": _mr(3, None, state="opened"),
        f"{base}/3/changes": FakeResponse(200, {"overflow": True, "changes_count": "1000+",
                                                "changes": [{"old_path": "x.jsp", "new_path": "x.jsp"}]}),
    }
    client, _ = _client(routes, ca_bundle="D:/ca.pem")
    changes, source = collect_from_mrs(client, "group/erp", [3])
    assert [c.path for c in changes] == ["x.jsp"]
    assert any("머지되지 않았" in w for w in source.warnings)
    assert any("overflow" in w for w in source.warnings)


def test_gitlab_errors_are_friendly():
    client, _ = _client({f"{API}/user": FakeResponse(401, {})})
    with pytest.raises(GitLabError, match="토큰"):
        client.current_user()
    client, _ = _client({})
    with pytest.raises(GitLabError, match="!9"):
        client.get_merge_request("group/erp", 9)
