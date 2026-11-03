"""새 버전 확인 (공유 폴더·URL 의 version.txt)."""
import http.server
import socket
import threading

import pytest

from shipkit import update


def test_version_compare():
    assert update.parse_version("0.11.0") == (0, 11, 0)
    assert update.parse_version(" v1.2 ") == (1, 2)
    assert update.parse_version("0.11.0-beta+3") == (0, 11, 0)
    assert update.parse_version("latest") == ()
    assert update.is_newer("0.11.0", "0.10.0") and update.is_newer("1.0", "0.99.9") and update.is_newer("0.10.1", "0.10")
    assert not update.is_newer("0.10.0", "0.10.0") and not update.is_newer("0.10", "0.10.0")
    assert not update.is_newer("0.9.9", "0.10.0") and not update.is_newer("latest", "0.10.0")


def test_check_folder(tmp_path):
    folder = tmp_path / "share"
    update.write_version_file(folder / "version.txt", "0.11.0", "서버 지문 잊기 추가\n자세한 내용은 가이드 참고")
    info = update.check(str(folder), current="0.10.0")
    assert info is not None and info.version == "0.11.0" and not info.is_url
    assert info.location == str(folder) and info.notes == "서버 지문 잊기 추가\n자세한 내용은 가이드 참고"
    assert update.check(str(folder / "version.txt"), current="0.10.0").version == "0.11.0"   # 파일을 직접 가리켜도 된다
    assert update.check(str(folder), current="0.11.0") is None                               # 같거나 새 버전이면 알리지 않는다
    assert update.check(str(folder), current="0.12.0") is None
    with pytest.raises(update.UpdateError, match="읽지 못했습니다"):
        update.check(str(tmp_path / "none"), current="0.10.0")
    (folder / "version.txt").write_text("\n", encoding="utf-8")
    with pytest.raises(update.UpdateError, match="첫 줄이 버전이 아닙니다"):
        update.check(str(folder), current="0.10.0")
    (folder / "version.txt").write_text("﻿v0.12.0\r\n", encoding="utf-8")               # BOM·CRLF 가 있어도 된다
    assert update.check(str(folder), current="0.10.0").version == "0.12.0"


def test_check_url(tmp_path):
    folder = tmp_path / "web"
    update.write_version_file(folder / "version.txt", "0.11.0", "notes")
    handler = type("H", (http.server.SimpleHTTPRequestHandler,),
                   {"__init__": lambda self, *a, **k: http.server.SimpleHTTPRequestHandler.__init__(
                       self, *a, directory=str(folder), **k), "log_message": lambda *a: None})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        info = update.check(base, current="0.10.0")
        assert info is not None and info.version == "0.11.0" and info.is_url and info.notes == "notes"
        assert update.check(base + "/version.txt", current="0.10.0").version == "0.11.0"
        assert update.check(base + "/", current="0.11.0") is None
        with pytest.raises(update.UpdateError, match="읽지 못했습니다"):
            update.check(base + "/missing/", current="0.10.0")                                 # 404
    finally:
        server.shutdown()
        server.server_close()
    with socket.socket() as s:                                                                  # 닫힌 포트: 연결 실패
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    with pytest.raises(update.UpdateError, match="읽지 못했습니다"):
        update.check(f"http://127.0.0.1:{port}/", current="0.10.0", timeout=1)
