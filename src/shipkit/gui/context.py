"""화면들이 함께 쓰는 상태: 설정 파일 위치, 읽어 들인 설정, 백그라운드 실행기, 메시지 창."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, List, Optional

from shipkit.config import Config, load_config
from shipkit.gui.widgets import Dialogs


@dataclass
class AppContext:
    config_path: Path
    runner: Any                       # TaskRunner (테스트에서는 SyncRunner)
    dialogs: Dialogs
    cfg: Optional[Config] = None
    load_error: Optional[str] = None
    listeners: List[Callable[[], None]] = field(default_factory=list)

    def reload(self) -> None:
        """설정을 다시 읽고 모든 화면에 알린다."""
        self.cfg, self.load_error = None, None
        if self.config_path.is_file():
            try:
                self.cfg = load_config(str(self.config_path))
            except Exception as e:  # 화면에서 오류를 보여주고 설정 화면으로 안내한다
                self.load_error = str(e)
        for listener in list(self.listeners):
            listener()
