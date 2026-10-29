"""화면들이 함께 쓰는 상태: 설정 파일 위치, 읽어 들인 설정, 지금 작업 중인 프로젝트, 백그라운드 실행기, 메시지 창."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, List, Optional

from PySide6.QtCore import QSettings

from shipkit.config import Config, ProjectConfig, load_config
from shipkit.gui.widgets import Dialogs


@dataclass
class AppContext:
    config_path: Path
    runner: Any                       # TaskRunner (테스트에서는 SyncRunner)
    dialogs: Dialogs
    cfg: Optional[Config] = None
    load_error: Optional[str] = None
    current: Optional[str] = None     # 지금 작업 중인 프로젝트 이름 (왼쪽 위 전환기)
    listeners: List[Callable[[], None]] = field(default_factory=list)          # 설정을 다시 읽었을 때
    project_listeners: List[Callable[[], None]] = field(default_factory=list)  # 작업 프로젝트가 바뀌었을 때
    connection_listeners: List[Callable[[], None]] = field(default_factory=list)  # 토큰을 저장·삭제했을 때

    @property
    def project(self) -> Optional[ProjectConfig]:
        return self.cfg.projects.get(self.current) if self.cfg and self.current else None

    def project_names(self) -> List[str]:
        return sorted(self.cfg.projects) if self.cfg else []

    def reload(self, select: Optional[str] = None) -> None:
        """설정을 다시 읽고 모든 화면에 알린다. select 를 주면 그 프로젝트로 바꾼다."""
        self.cfg, self.load_error = None, None
        if self.config_path.is_file():
            try:
                self.cfg = load_config(str(self.config_path))
            except Exception as e:  # 화면에서 오류를 보여주고 설정 화면으로 안내한다
                self.load_error = str(e)
        names = self.project_names()
        wanted = select or self.current or QSettings().value("project/current", "")
        self.current = wanted if wanted in names else (names[0] if names else None)
        for listener in list(self.listeners):
            listener()
        self._notify_project()

    def connections_changed(self) -> None:
        for listener in list(self.connection_listeners):
            listener()

    def set_project(self, name: Optional[str]) -> None:
        if name == self.current or (name is not None and name not in self.project_names()):
            return
        self.current = name
        self._notify_project()

    def _notify_project(self) -> None:
        if self.current:
            QSettings().setValue("project/current", self.current)
        for listener in list(self.project_listeners):
            listener()
