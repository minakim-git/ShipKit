"""보고서: 결과가 맨 위에 보이고, 필요한 내용만 중요한 순서로 담는다."""
import json
from datetime import datetime

from shipkit import history, report

from test_history import _package


def test_report_puts_result_first_and_drops_noise(maven_project, tmp_path):
    _, result = _package(maven_project, tmp_path / "out", ["src/main/java/com/acme/User.java",
                                                           "src/main/webapp/nope.jsp", "README.md"],
                         now=datetime(2026, 10, 29, 11, 40))
    md = (result.out_dir / "report.md").read_text(encoding="utf-8")
    lines = md.splitlines()
    assert lines[0] == "# erp 배포 패키지"
    assert lines[4] == "**❌ 실패 1개는 패키지에서 빠졌습니다**"                      # 결과 한 줄이 맨 위
    assert "| **3** | **0** | 1 | 0 | 1 | 1 |" in md                               # 반영·삭제·성공·경고·실패·제외
    order = [md.index(h) for h in ("## ⚠️ 확인 필요", "## 반영할 파일 (2개)", "## 제외된 파일 (1개)",
                                   "## 서버에 반영하는 방법")]
    assert order == sorted(order)
    assert "- ❌ `src/main/webapp/nope.jsp` — 로컬에 파일이 없습니다" in md
    assert "`WEB-INF/classes/com/acme/User$Inner.class`" in md
    for noise in ("파일 구조", "checksums", "Safe Deployment", "스크립트 없이"):    # 기존 툴에서 가져온 군더더기
        assert noise not in md

    data = json.loads((result.out_dir / history.RECORD_FILE).read_text(encoding="utf-8"))["report"]
    assert data["counts"] == {"ok": 1, "warn": 0, "error": 1, "skipped": 1} and data["applied"] == 3
    page = report.render_html(data)
    assert "확인 필요" in page and "패키지에서 빠짐" in page and "./shipkit.sh rollback" in page


def test_report_clean_package(maven_project, tmp_path):
    _, result = _package(maven_project, tmp_path / "out", ["src/main/webapp/WEB-INF/views/user/list.jsp"])
    md = (result.out_dir / "report.md").read_text(encoding="utf-8")
    assert "**✅ 문제 없이 만들어졌습니다**" in md
    assert "확인 필요" not in md and "제외된 파일" not in md and "따로 할 일" not in md
    assert report.verdict(report.build_data(result)) == ("success", "문제 없이 만들어졌습니다")
