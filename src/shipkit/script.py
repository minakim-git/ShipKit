"""서버 스크립트(SCRIPT_NAME) 생성. 줄바꿈은 항상 LF 로 고정한다."""
from __future__ import annotations

import os
from datetime import datetime
from importlib import resources
from pathlib import Path

from shipkit import SCRIPT_NAME, __app_name__, __tagline__, __version__

TEMPLATE_NAME = "server.sh"


def render(server_path: str, package_name: str, created_at: datetime) -> str:
    template = (resources.files("shipkit") / "templates" / TEMPLATE_NAME).read_text(encoding="utf-8")
    text = template.replace("\r\n", "\n")
    # server_path 는 작은따옴표 안에 들어간다. config 에서 따옴표와 셸 특수문자를 이미 거부한다.
    return (
        text.replace("@@SERVER_PATH@@", server_path)
        .replace("@@PACKAGE_NAME@@", package_name)
        .replace("@@CREATED_AT@@", created_at.strftime("%Y-%m-%d %H:%M:%S"))
        .replace("@@APP_NAME@@", __app_name__)
        .replace("@@APP_VERSION@@", __version__)
        .replace("@@TAGLINE@@", __tagline__)
        .replace("@@SCRIPT_NAME@@", SCRIPT_NAME)
    )


def write_deploy_script(path: Path, server_path: str, package_name: str, created_at: datetime) -> None:
    content = render(server_path, package_name, created_at)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)
    try:
        os.chmod(path, 0o755)
    except OSError:
        pass
