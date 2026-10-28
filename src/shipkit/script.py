"""서버 스크립트(SCRIPT_NAME) 생성. 줄바꿈은 항상 LF 로 고정한다."""
from __future__ import annotations

import os
from datetime import datetime
from importlib import resources
from pathlib import Path
from typing import Optional

from shipkit import CLI_NAME, SCRIPT_NAME, __app_name__, __tagline__, __version__
from shipkit.config import ServerOptions

TEMPLATE_NAME = "server.sh"


def render(server_path: str, package_name: str, created_at: datetime,
           server: Optional[ServerOptions] = None) -> str:
    server = server or ServerOptions()
    template = (resources.files("shipkit") / "templates" / TEMPLATE_NAME).read_text(encoding="utf-8")
    text = template.replace("\r\n", "\n")
    # 아래 값들은 작은따옴표 안에 들어간다. config 에서 따옴표와 셸 특수문자를 이미 거부한다.
    return (
        text.replace("@@SERVER_PATH@@", server_path)
        .replace("@@PACKAGE_NAME@@", package_name)
        .replace("@@CREATED_AT@@", created_at.strftime("%Y-%m-%d %H:%M:%S"))
        .replace("@@APP_NAME@@", __app_name__)
        .replace("@@APP_VERSION@@", __version__)
        .replace("@@TAGLINE@@", __tagline__)
        .replace("@@SCRIPT_NAME@@", SCRIPT_NAME)
        .replace("@@CLI_NAME@@", CLI_NAME)
        .replace("@@OWNER@@", server.owner or "")
        .replace("@@FILE_MODE@@", server.file_mode or "")
        .replace("@@DIR_MODE@@", server.dir_mode or "")
        .replace("@@HISTORY_FILE@@", server.history_file)
    )


def write_deploy_script(path: Path, server_path: str, package_name: str, created_at: datetime,
                        server: Optional[ServerOptions] = None) -> None:
    content = render(server_path, package_name, created_at, server)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)
    try:
        os.chmod(path, 0o755)
    except OSError:
        pass
