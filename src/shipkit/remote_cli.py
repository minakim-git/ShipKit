"""명령어로 서버에 바로 반영: shipkit deploy / shipkit server-status (Jenkins 같은 자동화용).

화면과 같은 규칙을 쓴다.
- 서버 지문: known_hosts 에 있으면 그대로, 처음이면 터미널에서 확인(자동화에서는 --accept-host-key), 바뀌었으면 거부
- 비밀번호: 환경변수(SHIPKIT_SSH_PASSWORD, SHIPKIT_RUN_PASSWORD) > OS 키링(화면에서 저장한 것) > 터미널 입력
- 결과는 패키지의 기록 파일에 남겨 화면의 기록 메뉴에서도 보인다
"""
from __future__ import annotations

import getpass
import sys
from pathlib import Path
from typing import List, Optional

from shipkit import history, remote
from shipkit.config import ProjectConfig, ServerProfile
from shipkit.secrets import SecretError, get_secret, set_secret

ENV_LOGIN = "SHIPKIT_SSH_PASSWORD"
ENV_RUN = "SHIPKIT_RUN_PASSWORD"
ACTIONS = ("check", "apply", "rollback")


class DeployError(Exception):
    pass


class _Tap:
    """내보낸 서버 출력을 모아 둔다 (check 결과 요약용)."""

    def __init__(self) -> None:
        self.parts: List[str] = []

    def text(self) -> str:
        return "".join(self.parts)


_tap = _Tap()


def _plain_out(text: str) -> None:
    sys.stdout.write(text)
    sys.stdout.flush()


def _out(text: str) -> None:
    _tap.parts.append(text)
    _plain_out(text)


def _tty() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def find_server(project: ProjectConfig, name: Optional[str]) -> ServerProfile:
    if not project.servers:
        raise DeployError(f"프로젝트 {project.name} 에 서버 연결(servers)이 없습니다. 화면의 프로젝트 설정 > 서버 연결에서 등록하세요.")
    if name is None:
        if len(project.servers) == 1:
            return project.servers[0]
        raise DeployError("--server 로 서버를 지정하세요: " + ", ".join(s.name for s in project.servers))
    for s in project.servers:
        if s.name == name:
            return s
    raise DeployError(f"설정에 없는 서버입니다: {name}. 등록된 서버: " + ", ".join(s.name for s in project.servers))


def find_package(project: ProjectConfig, path: Optional[str]) -> Path:
    """--package 를 주면 그 폴더, 아니면 가장 최근에 만든 패키지."""
    if path:
        folder = Path(path)
        if not history.is_package(folder):
            raise DeployError(f"패키지 폴더가 아닙니다 (manifest.txt 와 서버 스크립트가 있어야 합니다): {folder}")
        return folder
    records = history.list_records(project, limit=1)
    if not records:
        raise DeployError(f"만든 패키지가 없습니다: {project.output_dir}. 먼저 'shipkit package' 로 만드세요.")
    return records[0].path


def ensure_host_key(profile: ServerProfile, accept_new: bool) -> None:
    status = remote.check_host_key(profile)
    if status.state == "known":
        return
    if status.state == "changed":
        raise DeployError(f"{profile.host} 의 서버 지문이 기억한 것과 다릅니다. 다른 서버로 연결될 위험이 있어 접속하지 않습니다.\n"
                          f"지금 지문: {status.fingerprint}\n서버를 다시 설치했다면 {remote.known_hosts_path()} 에서 그 줄을 지우세요.")
    _out(f"{profile.name} ({profile.host}:{profile.port}) 에 처음 접속합니다. 서버 지문 ({status.key_type}): {status.fingerprint}\n")
    if not accept_new:
        if not _tty():
            raise DeployError("처음 접속하는 서버입니다. 지문을 확인했다면 --accept-host-key 를 붙이세요.")
        answer = input("이 서버가 맞으면 y 를 입력하세요 [y/N]: ").strip().lower()
        if answer not in ("y", "yes"):
            raise DeployError("서버 지문을 확인하지 않아 접속하지 않았습니다.")
    remote.remember_host_key(profile, status)
    _out("지문을 기억했습니다. 다음부터는 묻지 않습니다.\n")


def collect_secrets(profile: ServerProfile, env: dict, save: bool = False) -> remote.Secrets:
    """환경변수 > 키링 > 터미널 입력. 터미널이 아니고 아무 데도 없으면 실패한다."""
    found = {"login": None, "run": None}
    for kind, key, text in remote.secret_slots(profile):
        value = env.get(ENV_LOGIN if kind == "login" else ENV_RUN) or None
        if value is None:
            try:
                value = get_secret(key)
            except SecretError:
                value = None
        if value is None:
            if not _tty():
                raise DeployError(f"{text}이(가) 필요합니다. 환경변수 {ENV_LOGIN if kind == 'login' else ENV_RUN} 로 주거나, "
                                  "화면에서 한 번 접속해 '이 PC 에 저장' 하세요.")
            value = getpass.getpass(f"{profile.name} 서버: {text} (입력 내용은 표시되지 않음): ")
            if save:
                try:
                    set_secret(key, value)
                except SecretError as e:
                    _out(f"[경고] 비밀번호를 저장하지 못했습니다: {e}\n")
        found[kind] = value
    return remote.Secrets(login=found["login"], run=found["run"])


def _record(package: Path, profile: ServerProfile, project: ProjectConfig, res: remote.StepResult) -> None:
    try:
        history.append_deployment(package, profile.name, profile.address, res.action, res.code, res.remote_dir,
                                  profile.server_path or project.server_path)
    except OSError as e:
        _out(f"[경고] 반영 이력을 기록 파일에 남기지 못했습니다: {e}\n")


def connect(profile: ServerProfile, secrets: remote.Secrets) -> remote.Connection:
    try:
        import paramiko  # noqa: F401
    except ImportError as e:
        raise DeployError("서버 접속에는 paramiko 가 필요합니다: pip install \"shipkit[ssh]\"") from e
    return remote.SSHConnection(profile, secrets)


def deploy(project: ProjectConfig, profile: ServerProfile, package: Path, action: str, secrets: remote.Secrets,
           run_hooks: Optional[bool]) -> int:
    """check: 올리고 확인 / apply: 올리고 확인 뒤 backup·apply / rollback: 올려 둔 패키지 폴더에서 rollback. 반환: 종료 코드."""
    conn = connect(profile, secrets)
    try:
        if action == "rollback":
            remote_dir = remote.remote_package_dir(profile, package.name, conn.home())
            res = remote.run_action(conn, profile, secrets, remote_dir, "rollback", _out,
                                    yes=bool(run_hooks), no_hooks=not run_hooks)
            _record(package, profile, project, res)
            _out("\n되돌렸습니다.\n" if res.ok else f"\nrollback 실패 (종료 코드 {res.code})\n")
            return 0 if res.ok else 1

        last = [0]

        def progress(sent: int, total: int) -> None:
            pct = sent * 100 // total
            if pct >= last[0] + 25 or sent == total:
                last[0] = pct
                _out(f"  올리는 중 {pct}%\n")

        _tap.parts.clear()
        res = remote.upload_and_check(conn, profile, secrets, package, _out, progress)
        _record(package, profile, project, res)
        if not res.ok:
            _out(f"\n{res.action} 실패 (종료 코드 {res.code})\n")
            return 1
        checked = remote.parse_check(_tap.text())
        if checked is not None:
            n = checked.changes_server
            _out(f"\napply 때 바뀌는 파일 {n}개 (" + ", ".join(f"{k} {checked.counts.get(k, 0)}" for k in remote.CHECK_KINDS
                                                       if checked.counts.get(k)) + ")\n")
        if action == "check":
            _out("확인만 했습니다. 반영하려면 --apply 를 붙이세요.\n")
            return 0
        if run_hooks is None:
            raise DeployError("서버 명령(hooks)을 함께 실행할지 --hooks 또는 --no-hooks 로 정하세요.")
        res = remote.backup_and_apply(conn, profile, secrets, res.remote_dir, _out, run_hooks)
        _record(package, profile, project, res)
        _out("\n반영했습니다.\n" if res.ok else f"\n{res.action} 실패 (종료 코드 {res.code})\n")
        return 0 if res.ok else 1
    finally:
        conn.close()


def server_status(project: ProjectConfig, profile: ServerProfile, secrets: remote.Secrets, limit: int = 20) -> int:
    conn = connect(profile, secrets)
    try:
        entries = remote.fetch_history(conn, profile, secrets, project.server.history_file, _plain_out)
    finally:
        conn.close()
    target = profile.server_path or project.server_path
    current = remote.current_package(entries, target)
    _plain_out(f"서버 {profile.name} ({profile.address})  ·  대상 {target}\n")
    if current is None:
        _plain_out("반영한 기록이 없습니다.\n")
    elif current["action"] == "apply":
        _plain_out(f"지금 반영된 패키지: {current['package']}  ({current['at']}, {current['user']} 계정)\n")
    else:
        _plain_out(f"마지막 동작: {current['package']} 되돌림  ({current['at']}, {current['user']} 계정)\n")
    shown = entries[-limit:]
    if shown:
        _plain_out(f"\n최근 이력 {len(shown)}건 (오래된 것부터, {project.server.history_file}):\n")
        for e in shown:
            _plain_out(f"  {e['at']}  {e['user']:<10} {e['action']:<9} {e['target']}  [{e['package']}]  {e['result']}\n")
    return 0
