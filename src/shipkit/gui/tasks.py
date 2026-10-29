"""오래 걸리는 작업(GitLab 조회, 패키지 생성, 서버 올리기 등)을 백그라운드에서 돌려 창이 멈추지 않게 한다."""
from __future__ import annotations

from typing import Any, Callable, Optional, Set

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

from shipkit.config import ConfigError
from shipkit.remote import RemoteError
from shipkit.secrets import SecretError
from shipkit.sources.git_local import GitError
from shipkit.sources.github import GitHubError
from shipkit.sources.gitlab import GitLabError
from shipkit.sources.providers import ProviderError
from shipkit.workflow import InputError

# 사용자에게 메시지만 보여주면 되는 예상된 오류
EXPECTED_ERRORS = (ConfigError, SecretError, GitError, GitHubError, GitLabError, ProviderError, InputError,
                   RemoteError)

Emit = Callable[[Any], None]


def error_text(e: BaseException) -> str:
    if isinstance(e, EXPECTED_ERRORS):
        return str(e)
    return f"예상하지 못한 오류: {type(e).__name__}: {e}"


class _Signals(QObject):
    done = Signal(object)
    failed = Signal(str)
    progress = Signal(object)  # 진행 중 알림 (서버 출력 등): 화면 스레드에서 받는다


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
        self._start(lambda signals: fn, on_done, on_failed, quiet)

    def run_stream(self, fn: Callable[[Emit], Any], on_done: Callable[[Any], None],
                   on_failed: Optional[Callable[[str], None]], on_progress: Emit) -> None:
        """fn(emit) 을 백그라운드에서 실행한다. emit(값) 으로 보낸 것은 화면 스레드의 on_progress 로 온다."""
        self._start(lambda signals: (lambda: fn(signals.progress.emit)), on_done, on_failed, False, on_progress)

    def _start(self, make_fn, on_done, on_failed, quiet: bool, on_progress: Optional[Emit] = None) -> None:
        signals = _Signals()
        self._quiet.add(signals) if quiet else self._active.add(signals)
        if on_progress is not None:
            signals.progress.connect(on_progress)

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
        self._pool.start(_Task(make_fn(signals), signals))


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

    def run_stream(self, fn: Callable[[Emit], Any], on_done: Callable[[Any], None],
                   on_failed: Optional[Callable[[str], None]], on_progress: Emit) -> None:
        self.run(lambda: fn(on_progress), on_done, on_failed)
