"""오래 걸리는 작업(GitLab 조회, 패키지 생성 등)을 백그라운드에서 돌려 창이 멈추지 않게 한다."""
from __future__ import annotations

from typing import Any, Callable, Optional, Set

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

from shipkit.config import ConfigError
from shipkit.secrets import SecretError
from shipkit.sources.git_local import GitError
from shipkit.sources.github import GitHubError
from shipkit.sources.gitlab import GitLabError
from shipkit.sources.providers import ProviderError
from shipkit.workflow import InputError

# 사용자에게 메시지만 보여주면 되는 예상된 오류
EXPECTED_ERRORS = (ConfigError, SecretError, GitError, GitHubError, GitLabError, ProviderError, InputError)


def error_text(e: BaseException) -> str:
    if isinstance(e, EXPECTED_ERRORS):
        return str(e)
    return f"예상하지 못한 오류: {type(e).__name__}: {e}"


class _Signals(QObject):
    done = Signal(object)
    failed = Signal(str)


class _Task(QRunnable):
    def __init__(self, fn: Callable[[], Any], signals: _Signals):
        super().__init__()
        self.fn = fn
        self.signals = signals

    def run(self) -> None:
        try:
            result = self.fn()
        except Exception as e:  # 모든 오류를 화면으로 보낸다
            self.signals.failed.emit(error_text(e))
        else:
            self.signals.done.emit(result)


class TaskRunner(QObject):
    """fn 을 백그라운드에서 실행하고, 끝나면 화면 스레드에서 on_done / on_failed 를 부른다."""
    busy_changed = Signal(bool)

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._pool = QThreadPool.globalInstance()
        self._active: Set[_Signals] = set()
        self._quiet: Set[_Signals] = set()  # 신호 객체가 끝날 때까지 사라지지 않게 잡아 둔다

    def run(self, fn: Callable[[], Any], on_done: Callable[[Any], None],
            on_failed: Optional[Callable[[str], None]] = None, quiet: bool = False) -> None:
        """quiet: 화면의 버튼을 잠그지 않는 가벼운 작업 (예: 시작 전 점검)."""
        signals = _Signals()
        self._quiet.add(signals) if quiet else self._active.add(signals)

        def finish() -> None:
            if quiet:
                self._quiet.discard(signals)
                return
            self._active.discard(signals)
            if not self._active:
                self.busy_changed.emit(False)

        def done(result: Any) -> None:
            finish()
            on_done(result)

        def failed(message: str) -> None:
            finish()
            if on_failed:
                on_failed(message)

        signals.done.connect(done)
        signals.failed.connect(failed)
        if not quiet:
            self.busy_changed.emit(True)
        self._pool.start(_Task(fn, signals))


class SyncRunner(QObject):
    """테스트용: 같은 스레드에서 바로 실행한다."""
    busy_changed = Signal(bool)

    def run(self, fn: Callable[[], Any], on_done: Callable[[Any], None],
            on_failed: Optional[Callable[[str], None]] = None, quiet: bool = False) -> None:
        try:
            result = fn()
        except Exception as e:
            if on_failed:
                on_failed(error_text(e))
            return
        on_done(result)
