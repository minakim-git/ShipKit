"""GUI(PySide6): 화면 없이(offscreen) 창을 띄워 실제 위젯 조작으로 흐름을 확인한다."""
import os
import subprocess

import pytest
import yaml

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QSettings, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from shipkit import PROJECT_FILE, config_store, preflight  # noqa: E402
from shipkit.config import ConfigError  # noqa: E402
from shipkit.gui.app import (  # noqa: E402
    ADD_PROJECT, PAGE_CONNECTIONS, PAGE_DEPLOY, PAGE_HISTORY, PAGE_PROJECTS, MainWindow,
)
from shipkit.gui.setup_wizard import STEP_CONNECT, STEP_PROJECT, SetupWizard  # noqa: E402
from shipkit.gui.deploy_page import (  # noqa: E402
    STEP_DONE, STEP_PICK, STEP_REVIEW, TAB_BUILD, TAB_FILES, TAB_REQUESTS,
)
from shipkit.gui.report_view import ReportDialog  # noqa: E402
from shipkit.gui.tasks import SyncRunner  # noqa: E402
from shipkit.gui.widgets import META_ROLE  # noqa: E402
from shipkit.sources import providers  # noqa: E402

from conftest import write  # noqa: E402
from test_github import REPO, _client, _pr  # noqa: E402
from test_remote import sshd  # noqa: E402,F401  (테스트용 SSH 서버 fixture)
from conftest import FakeResponse  # noqa: E402

API = "https://api.github.com"


@pytest.fixture(scope="session")
def qapp(tmp_path_factory):
    QCoreApplication.setOrganizationName("ShipKitTest")
    QCoreApplication.setApplicationName("ShipKitTest")
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)  # 레지스트리 대신 임시 폴더에 저장
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path_factory.mktemp("qsettings")))
    return QApplication.instance() or QApplication([])


class FakeDialogs:
    def __init__(self, answer=True, password="tok"):
        self.messages = []
        self.answer = answer
        self.secret = password

    def info(self, title, text):
        self.messages.append(("info", title, text))

    def error(self, title, text):
        self.messages.append(("error", title, text))

    def ask(self, title, text):
        self.messages.append(("ask", title, text))
        return self.answer

    def password(self, title, text):
        return self.secret

    def ask_secret(self, title, text):  # 서버 비밀번호 입력: 물어본 문구를 남기고 (값, 저장) 을 돌려준다
        self.messages.append(("secret", title, text))
        return (self.secret, True) if self.secret is not None else None



def make_window(config_path, dialogs=None):
    return MainWindow(config_path=config_path, runner=SyncRunner(), dialogs=dialogs or FakeDialogs())


def _config(tmp_path, maven_project, **project):
    path = tmp_path / "shipkit.yaml"
    data = {"defaults": {"output_dir": (tmp_path / "out" / "{project}").as_posix()},
            "projects": {"erp": dict({"root": maven_project.as_posix(), "server_path": "/app/ROOT"}, **project)}}
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


# ---------------------------------------------------------------------- config_store
def test_config_store_save_validates_and_backs_up(tmp_path):
    path = tmp_path / "c.yaml"
    data = {"projects": {"a": {"root": "x", "output_dir": "y", "server_path": "/s", "hooks": {"post_apply": ["r"]}}}}
    config_store.save_raw(path, data)
    config_store.set_project(data, "a2", {"root": "z", "server_path": "/t", "output_dir": "o"}, old_name="a")
    assert data["projects"]["a2"]["hooks"] == {"post_apply": ["r"]}  # 화면에 없는 값은 보존
    config_store.save_raw(path, data)
    assert (tmp_path / "c.yaml.bak").is_file()
    with pytest.raises(ConfigError):
        config_store.save_raw(path, {"projects": {"bad": {"root": "x", "output_dir": "y", "server_path": "nope"}}})
    assert "a2" in config_store.load_raw(path)["projects"]            # 실패한 저장은 파일을 건드리지 않음
    with pytest.raises(ConfigError, match="이미 있습니다"):
        config_store.set_project(data, "a2", {"root": "q"})


def test_config_store_merge_legacy(tmp_path):
    from test_workflow import _legacy_db
    db = tmp_path / "database.db"
    _legacy_db(db)
    data = {"projects": {"acme_erp": {"root": "keep", "output_dir": "o", "server_path": "/k"}}}
    added, skipped = config_store.merge_legacy(data, db)
    assert added == [] and skipped == ["acme_erp"]
    assert data["projects"]["acme_erp"]["root"] == "keep" and data["gitlab"]["url"] == "https://gitlab.acme.local"


# ---------------------------------------------------------------------- 메인 창 / 설정
class FakeKeyring:
    def __init__(self):
        self.store = {}

    def get_password(self, service, key):
        return self.store.get((service, key))

    def set_password(self, service, key, value):
        self.store[(service, key)] = value

    def delete_password(self, service, key):
        del self.store[(service, key)]


class DrivenWizard(SetupWizard):
    """exec() 대신 테스트가 정한 조작(plan)을 실행하는 처음 실행 안내."""
    plan = None

    def exec(self):
        return type(self).plan(self)


def _drive(plan):
    return type("Driven", (DrivenWizard,), {"plan": staticmethod(plan)})


def test_first_run_wizard_new_user(qapp, tmp_path, monkeypatch, maven_project):
    monkeypatch.setattr("shipkit.legacy.legacy_db_path", lambda: tmp_path / "none.db")
    keyring = FakeKeyring()
    monkeypatch.setattr("shipkit.secrets._keyring", lambda: keyring)
    path = tmp_path / "new" / "shipkit.yaml"
    w = make_window(path)
    assert w.nav.currentRow() == PAGE_CONNECTIONS                  # 설정 파일이 없으면 연결부터
    for args in (["init", "-q"], ["remote", "add", "origin", "git@gitlab.acme.local:erp/web.git"]):
        subprocess.run(["git", "-C", str(maven_project), *args], check=True, capture_output=True)

    def plan(wz):
        assert not wz.legacy_card.isVisibleTo(wz) and wz.output_field.text().endswith("{project}")
        wz.next()                                                  # GitLab 주소 없음 -> 넘어가지 않음
        assert wz.step == STEP_CONNECT and "GitLab 주소" in wz.error.text()
        wz.gitlab_url.setText("https://gitlab.acme.local/")
        wz.token_edit.setText("tok-123")
        wz.next()
        wz.output_field.setText((tmp_path / "out" / "{project}").as_posix())
        assert wz.output_example.text().endswith("/out/erp/2026-10-29-1140")
        wz.next()
        assert wz.step == STEP_PROJECT and wz.next_btn.text() == "완료"
        wz.root_field.setText((maven_project / "src").as_posix())  # 하위 폴더를 골라도 저장소 최상위로
        assert wz.root_field.text() == maven_project.as_posix()
        assert wz.name_edit.text() == "erp" and wz.preset_combo.currentData() == "maven"
        assert wz.gitlab_project.text() == "erp/web"                # 원격 주소로 채움
        wz.next()                                                  # 서버 경로 없음
        assert "서버 배포 경로" in wz.error.text()
        wz.server_edit.setText("/app/ROOT")
        wz.next()
        return wz.result() == 1

    w.wizard_factory = _drive(plan)
    w.first_run()
    saved = config_store.load_raw(path)
    assert saved["gitlab"] == {"url": "https://gitlab.acme.local/"}
    assert saved["projects"]["erp"] == {"root": maven_project.as_posix(), "preset": "maven",
                                        "server_path": "/app/ROOT", "gitlab_project": "erp/web"}
    assert keyring.store[("shipkit", "https://gitlab.acme.local")] == "tok-123"   # 토큰은 키링에만
    assert "tok-123" not in path.read_text(encoding="utf-8")
    assert w.ctx.current == "erp" and w.nav.currentRow() == PAGE_DEPLOY


def test_first_run_wizard_imports_legacy_and_cancel(qapp, tmp_path, monkeypatch):
    from test_workflow import _legacy_db
    db = tmp_path / "database.db"
    _legacy_db(db)
    monkeypatch.setattr("shipkit.legacy.legacy_db_path", lambda: db)
    path = tmp_path / "new" / "shipkit.yaml"
    w = make_window(path)

    w.wizard_factory = _drive(lambda wz: False)                    # 나중에 하기
    w.first_run()
    assert not path.exists() and w.nav.currentRow() == PAGE_CONNECTIONS

    def plan(wz):
        assert wz.legacy_card.isVisibleTo(wz)
        wz.import_legacy()
        assert "acme_erp" in wz.imported
        assert wz.gitlab_url.text() == "https://gitlab.acme.local"
        wz.next()
        wz.output_field.setText((tmp_path / "out" / "{project}").as_posix())
        wz.next()
        assert wz.imported_note.isVisibleTo(wz)
        wz.next()                                                  # 프로젝트를 더 등록하지 않고 완료
        return wz.result() == 1

    w.wizard_factory = _drive(plan)
    w.settings_page.setup_requested.emit()                        # 앱 설정의 [처음 실행 안내 다시 보기]
    saved = config_store.load_raw(path)
    assert "acme_erp" in saved["projects"] and saved["defaults"]["output_dir"].endswith("/out/{project}")
    assert w.ctx.current == "acme_erp"


def test_connections_and_app_settings_save_their_own_keys(qapp, tmp_path, maven_project):
    path = _config(tmp_path, maven_project)
    w = make_window(path)
    c = w.connections_page
    c.gitlab.enabled.setChecked(True)
    c.gitlab.url.setText("https://gitlab.acme.local/")
    c.save()
    s = w.settings_page
    assert s.file_chip.text() == "있음" and str(path) == s.path_label.text()
    s.output_field.setText("D:/deploy/{project}")
    s.package_name.setText("%Y%m%d-%H%M")
    s.save()
    saved = config_store.load_raw(path)
    assert saved["gitlab"]["url"] == "https://gitlab.acme.local/" and "github" not in saved
    assert saved["defaults"] == {"output_dir": "D:/deploy/{project}", "package_name": "%Y%m%d-%H%M"}
    assert saved["projects"]["erp"]["server_path"] == "/app/ROOT"


# ---------------------------------------------------------------------- 프로젝트 화면
class FakeProjectDialog:
    def __init__(self, parent, name, entry, defaults, *, plan):
        self._plan = list(plan)
        self.entry = entry

    def exec(self):
        return bool(self._plan)

    def name(self):
        return self._plan[0][0]

    def values(self):
        name, values = self._plan.pop(0)
        return values


def test_projects_add_edit_delete(qapp, tmp_path, maven_project):
    path = _config(tmp_path, maven_project, hooks={"post_apply": ["restart"]}, source_encoding="cp949")
    dialogs = FakeDialogs()
    w = make_window(path, dialogs)
    page = w.projects_page
    assert page.name_edit.text() == "erp" and page.status_chip.text() == "설정 정상"
    assert page.hook_tables["post_apply"].rows() == [{"run": "restart", "when": ""}]

    root = tmp_path / "web"
    write(root / PROJECT_FILE, yaml.safe_dump({"preset": "vite", "server_path": "/var/www/web"}))
    page.dialog_factory = lambda *a: FakeProjectDialog(*a, plan=[
        ("web", {"root": "", "server_path": "/x"}),                    # 로컬 경로 없음 -> 오류 후 다시
        ("web", {"root": root.as_posix()}),                            # 나머지는 .shipkit.yml 을 따름
    ])
    page.add_project()
    assert any(m[0] == "error" and "root" in m[2] for m in dialogs.messages)
    assert w.ctx.cfg.projects["web"].preset == "vite" and w.ctx.current == "web" and page.loaded_name == "web"
    assert "팀 공유 · vite" in page.preset_combo.itemText(0) and page.mode_stack.currentIndex() == 1  # sync
    assert page.server_edit.placeholderText() == "팀 공유: /var/www/web"

    w.ctx.set_project("erp")
    page.server_edit.setText("/app/NEW")
    assert page.dirty and page.save_btn.isEnabled() and page.status_chip.text() == "저장하지 않음"
    page.server_edit.setText("relative/path")                      # 잘못된 값은 저장할 수 없다
    assert page.status_chip.text() == "설정 오류" and not page.save_btn.isEnabled()
    page.server_edit.setText("/app/NEW")
    page.name_edit.setText("erp2")                                 # 이름 바꾸기
    assert page.save()
    raw = config_store.load_raw(path)["projects"]
    assert "erp" not in raw and raw["erp2"]["server_path"] == "/app/NEW" and w.ctx.current == "erp2"
    assert raw["erp2"]["hooks"] == {"post_apply": ["restart"]} and raw["erp2"]["source_encoding"] == "cp949"
    assert not page.dirty and list(raw) == ["erp2", "web"]
    page.server_edit.setText("/app/X")
    page.server_edit.setText("/app/NEW")                           # 원래 값으로 돌리면 바뀐 것 없음
    assert not page.dirty and not page.save_btn.isEnabled()

    page.server_edit.setText("/app/OTHER")                         # 저장하지 않고 다른 프로젝트로 -> 묻는다
    dialogs.answer = False
    w.ctx.set_project("web")
    assert dialogs.messages[-1][1] == "저장하지 않은 변경" and page.loaded_name == "web" and not page.dirty
    assert config_store.load_raw(path)["projects"]["erp2"]["server_path"] == "/app/NEW"

    dialogs.answer = True
    page.delete_project()
    assert "web" not in w.ctx.cfg.projects and w.ctx.current == "erp2"


def test_project_rules_override_and_path_test(qapp, tmp_path, maven_project):
    path = _config(tmp_path, maven_project)
    w = make_window(path)
    page = w.projects_page
    rules = page.rules_table
    assert page.rules_override.source == "프리셋 maven" and not rules.table.isEnabled()
    assert [r["dest"] for r in rules.rows()] == ["WEB-INF/classes", "WEB-INF/classes", "."]

    page.test_input.setPlainText("src/main/java/com/acme/User.java\nsrc/main/webapp/WEB-INF/views/user/list.jsp\n"
                                 "D\tsrc/main/webapp/old.jsp\nREADME.md")
    page.run_path_test()
    tree = page.test_tree
    items = {tree.topLevelItem(i).text(1).split("  ", 1)[1]: tree.topLevelItem(i) for i in range(tree.topLevelItemCount())}
    user = items["src/main/java/com/acme/User.java"]
    assert user.text(0) == "성공" and "→  /app/ROOT/WEB-INF/classes/com/acme/User$Inner.class" in [
        user.child(i).text(2) for i in range(user.childCount())]
    assert items["src/main/webapp/old.jsp"].child(0).text(2) == "(삭제)  /app/ROOT/old.jsp"
    assert items["README.md"].text(0) == "제외"

    page.rules_override.check.setChecked(True)                   # 직접 지정: 프리셋 규칙을 복사해 고친다
    assert rules.table.isEnabled() and len(rules.rows()) == 3
    rules.table.item(2, 1).setText("static")
    page.run_path_test()
    jsp = [tree.topLevelItem(i) for i in range(tree.topLevelItemCount())
           if tree.topLevelItem(i).text(1).endswith("list.jsp")][0]
    assert jsp.child(0).text(2) == "→  /app/ROOT/static/WEB-INF/views/user/list.jsp"   # 저장 전 값으로 시험
    assert page.save()
    saved = config_store.load_raw(path)["projects"]["erp"]["rules"]
    assert saved[2] == {"src": "src/main/webapp", "dest": "static"} and saved[0]["build_dir"] == "target/classes"
    assert w.ctx.project.rules[2].dest == "static"

    page.rules_override.check.setChecked(False)                  # 다시 프리셋을 따름
    assert page.save() and "rules" not in config_store.load_raw(path)["projects"]["erp"]


def test_project_shared_file_values_and_hook_override(qapp, tmp_path, maven_project):
    write(maven_project / PROJECT_FILE, yaml.safe_dump({"server_path": "/app/SHARED",
                                                        "hooks": {"post_apply": ["restart"]}}))
    path = tmp_path / "shipkit.yaml"
    path.write_text(yaml.safe_dump({"defaults": {"output_dir": (tmp_path / "out").as_posix()},
                                    "projects": {"erp": {"root": maven_project.as_posix()}}}), encoding="utf-8")
    w = make_window(path)
    page = w.projects_page
    assert page.server_edit.placeholderText() == "팀 공유: /app/SHARED" and page.basic_note.isVisibleTo(page)
    assert page.hooks_override.source == "팀 공유" and not page.hook_tables["post_apply"].table.isEnabled()
    assert page.hook_tables["post_apply"].rows() == [{"run": "restart", "when": ""}]
    assert page.hook_tables["pre_apply"].table.placeholder.startswith("예: ")

    page.hooks_override.check.setChecked(True)                    # 이 PC 에서는 서버 명령을 끈다
    page.hook_tables["post_apply"].table.selectRow(0)
    page.hook_tables["post_apply"].remove_selected()
    assert page.save() and config_store.load_raw(path)["projects"]["erp"]["hooks"] == {}
    assert w.ctx.project.hooks == {} and w.ctx.project.server_path == "/app/SHARED"


# ---------------------------------------------------------------------- 배포 화면
def test_deploy_files_preview_and_make(qapp, tmp_path, maven_project):
    dialogs = FakeDialogs()
    w = make_window(_config(tmp_path, maven_project), dialogs)
    d = w.deploy_page
    assert w.nav.currentRow() == PAGE_DEPLOY and d.project.name == "erp"
    assert not d.tabs.isTabVisible(TAB_REQUESTS) and not d.tabs.isTabVisible(TAB_BUILD)
    assert d.tabs.currentIndex() == TAB_FILES

    assert d.step == STEP_PICK and d.last_checks                 # 시작 전 점검이 돌았다
    d.files_edit.setPlainText("src/main/java/com/acme/User.java\nsrc/main/webapp/WEB-INF/views/user/list.jsp\n")
    assert d.pick_summary.text() == "파일 2개"
    d.preview()
    assert d.step == STEP_REVIEW and d.make_btn.isEnabled() and d.result_tree.topLevelItemCount() == 2
    assert "서버 반영 4개" in d.summary_label.text()
    user = d.result_tree.findItems("User.java", Qt.MatchFlag.MatchEndsWith, 2)[0]
    children = [user.child(i).text(2) for i in range(user.childCount())]  # 소스 아래에 서버 파일이 한 줄씩
    assert "→  WEB-INF/classes/com/acme/User.class" in children
    assert "→  WEB-INF/classes/com/acme/User$Inner.class" in children and user.isExpanded()

    d.go_step(STEP_PICK)                             # 이전으로 돌아가 입력을 바꾸면 다시 미리보기 해야 함
    d.files_edit.appendPlainText("pom.xml")
    assert not d.make_btn.isEnabled() and not d.stepper.buttons[STEP_REVIEW].isEnabled()
    d.preview()
    assert any("pom.xml" in d.messages.item(i).text() for i in range(d.messages.count()))  # 알림

    d.make_package()
    pkg = d.last_result.out_dir
    assert d.step == STEP_DONE and (pkg / "files/WEB-INF/classes/com/acme/User.class").is_file()
    assert str(pkg) in d.done_label.text()
    assert d.done_commands == [f"cd ~/deploy/{pkg.name}", "chmod +x shipkit.sh", "./shipkit.sh check",
                               "./shipkit.sh backup", "./shipkit.sh apply", "./shipkit.sh rollback"]
    d.start_over()
    assert d.step == STEP_PICK and d.files_edit.toPlainText() == ""


def test_report_dialog_and_pull_button(qapp, tmp_path, maven_project, monkeypatch):
    dialogs = FakeDialogs()
    w = make_window(_config(tmp_path, maven_project), dialogs)
    d = w.deploy_page
    d.files_edit.setPlainText("src/main/java/com/acme/User.java")
    d.preview()
    d.make_package()
    d.open_report_btn.click()                                   # 보고서는 ShipKit 안의 창으로
    dialog = [x for x in QApplication.topLevelWidgets() if isinstance(x, ReportDialog)][-1]
    assert dialog.path == d.last_result.out_dir / "report.md" and dialog.isVisible()
    assert "User.class" in dialog.browser.toPlainText() and "| " not in dialog.browser.toPlainText()  # 표로 그림
    dialog.close()

    pulled = []
    monkeypatch.setattr("shipkit.sources.git_local.pull_ff", lambda root: pulled.append(root) or 2)
    d.start_over()
    d._show_checks([preflight.Check("warning", "원격보다 2커밋 뒤처짐", "", preflight.ACTION_PULL)])
    assert d.pull_btn.isVisibleTo(d)
    d.files_edit.setPlainText("src/main/java/com/acme/User.java")
    d.preview()
    d.pull_btn.click()                                          # 받은 뒤에는 옛 미리보기를 지우고 ① 로
    assert pulled == [maven_project] and d.step == STEP_PICK and d.prepared is None
    assert "커밋 2개" in dialogs.messages[-1][2] and "다시 빌드" in dialogs.messages[-1][2]
    assert not d.pull_btn.isVisibleTo(d)                        # 다시 점검 (git 저장소 아님)


def test_deploy_failure_asks_before_making(qapp, tmp_path, maven_project):
    dialogs = FakeDialogs(answer=False)
    w = make_window(_config(tmp_path, maven_project), dialogs)
    d = w.deploy_page
    d.files_edit.setPlainText("src/main/webapp/WEB-INF/views/user/list.jsp\nsrc/main/webapp/nope.jsp")
    d.preview()
    assert "실패 1" in d.summary_label.text()
    d.filter_buttons["error"].click()                # 실패만 걸러 보기
    tree = d.result_tree
    visible = [tree.topLevelItem(i).text(2) for i in range(tree.topLevelItemCount()) if not tree.topLevelItem(i).isHidden()]
    assert visible == ["src/main/webapp/nope.jsp"]
    assert not d.filter_buttons["warn"].isVisibleTo(w)  # 해당 항목이 없는 상태 버튼은 숨김
    d.make_package()
    assert dialogs.messages[-1][0] == "ask" and d.last_result is None and d.step == STEP_REVIEW
    dialogs.answer = True
    d.make_package()
    assert (d.last_result.out_dir / "manifest.txt").read_text(encoding="utf-8") == "WEB-INF/views/user/list.jsp\n"


def test_deploy_github_pr_flow(qapp, tmp_path, maven_project, monkeypatch):
    routes = {
        f"{API}/repos/{REPO}/pulls": FakeResponse(200, [_pr(12, merged_at="2026-10-05T00:00:00Z", title="JSP")]),
        f"{API}/repos/{REPO}/pulls/12": FakeResponse(200, _pr(12, merged_at="2026-10-05T00:00:00Z")),
        f"{API}/repos/{REPO}/pulls/12/files": [FakeResponse(200, [
            {"filename": "src/main/webapp/WEB-INF/views/user/list.jsp", "status": "modified"}])],
    }
    monkeypatch.setattr(providers, "github_client", lambda cfg: _client(routes)[0])
    path = _config(tmp_path, maven_project, github_repo=REPO)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["github"] = {}
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    w = make_window(path)
    d = w.deploy_page
    assert d.tabs.isTabVisible(TAB_REQUESTS) and d.tabs.currentIndex() == TAB_REQUESTS
    d.load_requests()
    assert d.req_table.rowCount() == 1 and d.req_table.item(0, 1).text() == "#12"
    d.preview()                                      # 아무것도 안 고르면 안내
    assert w.ctx.dialogs.messages[-1][2].startswith("배포할 MR/PR")
    d.req_table.item(0, 0).setCheckState(Qt.CheckState.Checked)
    d.preview()
    assert "GitHub PR #12" in d.summary_label.text()
    d.make_package()
    report = (d.last_result.out_dir / "report.md").read_text(encoding="utf-8")
    assert "## Pull Request" in report


def test_deploy_sync_project_defaults_to_build_tab(qapp, tmp_path):
    root = tmp_path / "web"
    write(root / "dist/index.html", "<html/>")
    write(root / PROJECT_FILE, yaml.safe_dump({"preset": "vite", "server_path": "/var/www/web"}))
    path = tmp_path / "c.yaml"
    path.write_text(yaml.safe_dump({"defaults": {"output_dir": (tmp_path / "out").as_posix()},
                                    "projects": {"web": {"root": root.as_posix()}}}), encoding="utf-8")
    w = make_window(path)
    d = w.deploy_page
    assert d.tabs.currentIndex() == TAB_BUILD and "빌드 결과 동기화" in d.mode_label.text()
    d.archive_check.setChecked(True)
    d.preview()
    d.make_package()
    assert d.last_result.archive.name.endswith(".tar.gz")


def test_project_switcher(qapp, tmp_path, maven_project):
    path = _config(tmp_path, maven_project)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["projects"]["alpha"] = {"root": maven_project.as_posix(), "server_path": "/app/ALPHA"}
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    w = make_window(path)
    sw = w.project_switcher
    assert [sw.itemData(i) for i in range(2)] == ["alpha", "erp"] and sw.findData(ADD_PROJECT) > 0
    sw.activated.emit(sw.findData("erp"))
    assert w.ctx.current == "erp"
    sw.activated.emit(sw.findData("alpha"))
    assert w.ctx.current == "alpha" and w.deploy_page.project.server_path == "/app/ALPHA"
    assert sw.itemData(sw.currentIndex(), META_ROLE) == "maven  ·  /app/ALPHA"
    assert "서버 경로 /app/ALPHA" in w.deploy_page.mode_label.text()

    opened = []
    w.projects_page.add_project = lambda: opened.append(True)
    sw.activated.emit(sw.findData(ADD_PROJECT))      # 동작 항목: 프로젝트 추가 창을 열고, 선택은 그대로
    assert opened and w.nav.currentRow() == PAGE_PROJECTS and sw.currentData() == "alpha"


def test_history_lists_packages_and_remakes(qapp, tmp_path, maven_project):
    w = make_window(_config(tmp_path, maven_project))
    d, h = w.deploy_page, w.history_page
    w.go(PAGE_HISTORY)
    assert h.body.currentWidget() is h.empty                  # 아직 패키지가 없음
    w.go(PAGE_DEPLOY)
    d.files_edit.setPlainText("src/main/java/com/acme/User.java\nD\tsrc/main/webapp/old.jsp")
    d.preview()
    d.make_package()
    w.go(PAGE_HISTORY)                                       # 화면을 열면 새로 만든 패키지가 보인다
    assert h.list.count() == 1 and h.current.path == d.last_result.out_dir
    assert h.tabs.tabText(0) == "파일 4" and h.files.item(3).text() == "(삭제) old.jsp"
    h.search.setText("user$inner")                           # 파일 경로로 찾기
    assert not h.list.item(0).isHidden()
    h.search.setText("nothing-like-this")
    assert h.list.item(0).isHidden() and h.count_label.text() == "0개 찾음 / 전체 1개"

    h.remake_btn.click()                                     # 같은 내용으로 다시 만들기 -> 배포 ① 에 채움
    assert w.nav.currentRow() == PAGE_DEPLOY and d.step == STEP_PICK and d.tabs.currentIndex() == TAB_FILES
    assert d.files_edit.toPlainText().splitlines() == ["M\tsrc/main/java/com/acme/User.java", "D\tsrc/main/webapp/old.jsp"]


def test_theme_switch(qapp, tmp_path, maven_project):
    from shipkit.gui import theme
    w = make_window(_config(tmp_path, maven_project))
    try:
        w.settings_page.set_theme("dark")                      # 앱 설정에서 바로 바뀌고 이 PC 에 기억
        assert QSettings().value("ui/theme") == "dark" and theme.is_dark()
        assert theme.DARK["BG"] in qapp.styleSheet() and qapp.palette().window().color().name() == theme.DARK["BG"].lower()
        arrow = theme.asset("chevron-down.svg")                 # 스타일시트 그림도 어두운 색 사본
        assert "shipkit-theme" in arrow and theme.DARK["MUTED"] in open(arrow, encoding="utf-8").read()
        assert w.settings_page.theme_buttons["dark"].isChecked()
    finally:
        w.settings_page.set_theme("light")
        QSettings().remove("ui/theme")
    assert not theme.is_dark() and theme.LIGHT["BG"] in qapp.styleSheet()


def test_remote_panel_cancel(qapp, tmp_path, maven_project, monkeypatch):
    """[취소]는 진행 중일 때만 보이고, 끊긴 작업은 오류 창 없이 이전 상태로 돌아온다."""
    from shipkit import remote

    class FakeConn:
        def __init__(self, profile, secrets):
            self.cancelled = False

        def cancel(self):
            self.cancelled = True

        def close(self):
            pass

    panel_ref = {}

    def step(conn, output, progress):
        output("올리는 중...\n")
        panel = panel_ref["panel"]
        assert panel.cancel_btn.isVisibleTo(panel) and panel.cancel_btn.isEnabled()
        panel.cancel_btn.click()                                 # 작업 중에 [취소]
        assert conn.cancelled and panel.cancel_btn.text() == "중단 중…"
        raise remote.RemoteCancelled("취소했습니다.")

    dialogs = FakeDialogs(password="pw")
    path = _config(tmp_path, maven_project, servers=[{"name": "개발", "host": "10.0.0.5", "user": "deployer"}])
    w = make_window(path, dialogs)
    d = w.deploy_page
    d.files_edit.setPlainText("src/main/webapp/WEB-INF/views/user/list.jsp")
    d.preview()
    d.make_package()
    panel = d.remote_panel
    panel_ref["panel"] = panel
    panel.client.connection_factory = FakeConn
    panel.client.host_key_checker = lambda profile: remote.HostKeyStatus("known", "ssh-ed25519", "SHA256:x")
    assert not panel.cancel_btn.isVisibleTo(panel)
    assert not panel.client.cancel()                             # 진행 중인 작업이 없으면 끊을 것도 없다

    panel.client.stream = _wrap_stream(panel.client, step)
    panel.upload_and_check()
    assert panel.stage == "ready" and not panel.cancel_btn.isVisibleTo(panel)
    assert "[취소] 취소했습니다." in panel.log.toPlainText()
    assert not [m for m in dialogs.messages if m[0] == "error"]  # 취소는 오류가 아니다


def _wrap_stream(client, step):
    """RemoteClient.stream 에 들어가는 단계를 테스트용 step 으로 바꾼다 (지문·비밀번호 처리는 그대로)."""
    original = client.stream

    def stream(profile, secrets, _step, *args, **kwargs):
        return original(profile, secrets, step, *args, **kwargs)
    return stream


def test_remote_panel_upload_apply_rollback(qapp, tmp_path, maven_project, monkeypatch, sshd):
    from sshd import LOGIN_PW
    from test_remote import bash_path
    keyring = FakeKeyring()
    monkeypatch.setattr("shipkit.secrets._keyring", lambda: keyring)
    monkeypatch.setattr("shipkit.remote.known_hosts_path", lambda: tmp_path / "known_hosts")
    server = tmp_path / "server"
    write(server / "WEB-INF/views/user/list.jsp", "OLD")
    path = _config(tmp_path, maven_project, server_path=bash_path(server), servers=[
        {"name": "개발", "host": "127.0.0.1", "port": sshd.port, "user": "deployer"}])
    dialogs = FakeDialogs(password=LOGIN_PW)
    w = make_window(path, dialogs)
    d = w.deploy_page
    d.files_edit.setPlainText("src/main/webapp/WEB-INF/views/user/list.jsp")
    d.preview()
    d.make_package()
    panel = d.remote_panel
    assert panel.main.isVisibleTo(panel) and panel.server_combo.currentText().startswith("개발")

    panel.upload_and_check()                                    # 처음: 지문 확인 + 비밀번호 입력(저장)
    assert panel.stage == "checked", panel.log.toPlainText()
    assert [m[1] for m in dialogs.messages if m[0] in ("ask", "secret")] == ["새 서버 확인", "비밀번호 입력"]
    assert keyring.store[("shipkit", f"ssh:login:deployer@127.0.0.1:{sshd.port}")] == LOGIN_PW
    panel.apply()
    assert panel.stage == "applied", panel.log.toPlainText()
    assert (server / "WEB-INF/views/user/list.jsp").read_text(encoding="utf-8") == "<%-- jsp --%>"
    asked = len([m for m in dialogs.messages if m[0] == "secret"])
    panel.rollback()                                            # 저장한 비밀번호를 써서 다시 묻지 않음
    assert panel.stage == "rolled_back" and (server / "WEB-INF/views/user/list.jsp").read_text(encoding="utf-8") == "OLD"
    assert len([m for m in dialogs.messages if m[0] == "secret"]) == asked

    keyring.store[("shipkit", f"ssh:login:deployer@127.0.0.1:{sshd.port}")] = "wrong"
    panel.upload_and_check()                                    # 저장한 비밀번호가 틀리면 지우고 알린다
    assert dialogs.messages[-1][0] == "error" and "로그인에 실패" in dialogs.messages[-1][2]
    assert ("shipkit", f"ssh:login:deployer@127.0.0.1:{sshd.port}") not in keyring.store

    # 프로젝트 설정의 서버 연결 탭: 목록·시험
    w.go(PAGE_PROJECTS)
    page = w.projects_page
    assert page.server_list.count() == 1 and page.srv_host.text() == "127.0.0.1"
    page.srv_mode.setCurrentIndex(1)                            # 다른 계정으로 실행 -> 칸이 나타남
    assert page.server_form.isRowVisible(page.srv_run_as)
    page.srv_mode.setCurrentIndex(0)
    page.test_server()
    assert dialogs.messages[-1][:2] == ("info", "연결 시험") and "접속 성공" in dialogs.messages[-1][2], dialogs.messages[-1]
    page.add_server()
    page.srv_host.setText("10.0.0.9")
    page.srv_user.setText("app")
    assert page.save()
    assert [s["name"] for s in config_store.load_raw(path)["projects"]["erp"]["servers"]] == ["개발", "서버 2"]
