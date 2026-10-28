"""서버 스크립트 생성.

- 리눅스(SCRIPT_NAME, bash): 줄바꿈은 항상 LF (CRLF 면 bad interpreter 오류)
- Windows(SCRIPT_NAME_PS, PowerShell): UTF-8 BOM + CRLF (Windows PowerShell 5.1 이 한글을 깨뜨리지 않도록)
"""
from __future__ import annotations

import os
from datetime import datetime
from importlib import resources
from pathlib import Path
from typing import Optional

from shipkit import CLI_NAME, SCRIPT_NAME, SCRIPT_NAME_PS, __app_name__, __tagline__, __version__
from shipkit.config import ServerOptions

TEMPLATE_NAME = "server.sh"
TEMPLATE_NAME_PS = "server.ps1"


def render(server_path: str, package_name: str, created_at: datetime,
           server: Optional[ServerOptions] = None, server_os: str = "linux") -> str:
    server = server or ServerOptions()
    windows = server_os == "windows"
    template = (resources.files("shipkit") / "templates" / (TEMPLATE_NAME_PS if windows else TEMPLATE_NAME)
                ).read_text(encoding="utf-8")
    text = template.replace("\r\n", "\n")
    # 아래 값들은 작은따옴표 안에 들어간다. config 에서 따옴표와 셸 특수문자를 이미 거부한다.
    return (
        text.replace("@@SERVER_PATH@@", server_path)
        .replace("@@PACKAGE_NAME@@", package_name)
        .replace("@@CREATED_AT@@", created_at.strftime("%Y-%m-%d %H:%M:%S"))
        .replace("@@APP_NAME@@", __app_name__)
        .replace("@@APP_VERSION@@", __version__)
        .replace("@@TAGLINE@@", __tagline__)
        .replace("@@SCRIPT_NAME@@", SCRIPT_NAME_PS if windows else SCRIPT_NAME)
        .replace("@@CLI_NAME@@", CLI_NAME)
        .replace("@@OWNER@@", server.owner or "")
        .replace("@@FILE_MODE@@", server.file_mode or "")
        .replace("@@DIR_MODE@@", server.dir_mode or "")
        .replace("@@HISTORY_FILE@@", server.history_file)
    )


def write_deploy_script(path: Path, server_path: str, package_name: str, created_at: datetime,
                        server: Optional[ServerOptions] = None, server_os: str = "linux") -> None:
    content = render(server_path, package_name, created_at, server, server_os)
    if server_os == "windows":
        with open(path, "w", encoding="utf-8-sig", newline="\r\n") as f:
            f.write(content)
        return
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)
    try:
        os.chmod(path, 0o755)
    except OSError:
        pass
