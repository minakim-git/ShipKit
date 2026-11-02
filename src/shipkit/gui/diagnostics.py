"""오류 기록: 예상하지 못한 오류를 로그 파일에 남기고 창으로 알린다.

exe 는 콘솔이 없어서, 화면 스레드에서 난 예외를 그냥 두면 stderr 로 사라지고 사용자에게는 '눌렀는데 아무 일도 없음'으로
보인다. 그래서 sys.excepthook 을 바꿔 (PySide 는 슬롯의 예외를 여기로 보낸다) 로그에 남기고 창을 띄운다.
백그라운드 작업(tasks.py)의 오류도 같은 로그에 남긴다.
"""
from __future__ import annotations

import logging
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable, Optional

from shipkit import __app_name__, __version__
from shipkit.config import user_config_path

LOG_NAME = "shipkit.log"
log = logging.getLogger("shipkit")

_handler: Optional[logging.Handler] = None
_path: Optional[Path] = None
_showing = False


def log_path() -> Path:
    """설정 파일 옆의 로그 파일 (%APPDATA%\\shipkit\\shipkit.log)."""
    return _path or user_config_path().parent / LOG_NAME


def setup_logging(path: Optional[Path] = None) -> Optional[Path]:
    """로그 파일을 연다 (1MB 씩 3개까지 돌려 쓴다). 폴더를 만들지 못하면 None 을 돌려주고 로그 없이 계속한다."""
    global _handler, _path
    close_logging()
    path = path or log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(path, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    except OSError:
        return None
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    _handler, _path = handler, path
    log.info("%s v%s 시작", __app_name__, __version__)
    return path


def close_logging() -> None:
    global _handler, _path
    if _handler is not None:
        log.removeHandler(_handler)
        _handler.close()
    _handler, _path = None, None


def install_excepthook(dialogs: Callable[[], Optional[object]]) -> None:
    """처리되지 않은 예외를 로그에 남기고 창으로 알린다.

    dialogs() 는 창이 준비된 뒤에만 Dialogs 를 돌려준다 (아직 없으면 None → 기본 처리기로 stderr 에만 출력).
    """
    def hook(exc_type, exc, tb) -> None:
        global _showing
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        log.error("처리되지 않은 오류", exc_info=(exc_type, exc, tb))
        if _showing:  # 창을 띄우는 중에 또 나면 로그로만
            return
        _showing = True
        try:
            d = dialogs()
            if d is None:
                sys.__excepthook__(exc_type, exc, tb)
            else:
                d.error("예상하지 못한 오류",
                        f"{exc_type.__name__}: {exc}\n\n자세한 내용은 로그 파일에 남겼습니다.\n{log_path()}\n\n"
                        "앱 설정 → 정보의 [로그 파일 열기]로 볼 수 있습니다. 이 파일과 오류 직전에 한 일을 담당자에게 알려 주세요.")
        except Exception:
            sys.__excepthook__(exc_type, exc, tb)
        finally:
            _showing = False

    def thread_hook(args) -> None:  # 작업 실행기 밖의 스레드 (보통 없음): 로그로만
        name = args.thread.name if args.thread else "?"
        log.error("스레드 %s 에서 처리되지 않은 오류", name, exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = hook
    threading.excepthook = thread_hook


def uninstall_excepthook() -> None:
    sys.excepthook = sys.__excepthook__
    threading.excepthook = threading.__excepthook__
