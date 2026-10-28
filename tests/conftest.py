from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Dict, Iterable

import pytest

from shipkit.config import parse_config


def write(path: Path, content: str = "x", mtime: float = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


@pytest.fixture
def maven_project(tmp_path: Path) -> Path:
    """빌드가 끝난 Maven 웹 프로젝트 모형."""
    root = tmp_path / "erp"
    old, new = time.time() - 100, time.time()
    java = root / "src/main/java/com/acme"
    classes = root / "target/classes/com/acme"
    write(java / "User.java", "package com.acme;\npublic class User {\n  static class Inner {}\n}\n", old)
    write(java / "UserService.java", "package com.acme;\npublic class UserService {}\n", old)
    write(java / "Multi.java", "package com.acme;\n// class Commented {}\npublic class Multi {}\nclass Helper {}\n", old)
    for name in ["User", "User$Inner", "User$1", "UserService", "Multi", "Helper", "Helper$1"]:
        write(classes / f"{name}.class", "bytecode", new)
    write(root / "src/main/resources/mapper/user.xml", "<mapper/>")
    write(root / "src/main/webapp/WEB-INF/views/user/list.jsp", "<%-- jsp --%>")
    write(root / "src/main/webapp/js/한글 파일.js", "// js")
    write(root / "pom.xml", "<project/>")
    write(root / "README.md", "readme")
    return root


def make_config(root: Path, output: Path, **project_overrides) -> "Config":
    project = {
        "root": str(root),
        "output_dir": str(output),
        "server_path": "/app/tomcat/webapps/ROOT",
        "exclude": ["src/test/**", "**/*.md"],
    }
    project.update(project_overrides)
    return parse_config({"projects": {"erp": project}})


class FakeResponse:
    def __init__(self, status: int = 200, data=None, headers: Dict[str, str] = None):
        self.status_code = status
        self._data = data
        self.headers = headers or {}
        self.text = str(data)

    def json(self):
        return self._data


class FakeSession:
    """URL(쿼리 제외)과 page 파라미터로 응답을 돌려주는 requests.Session 대역."""

    def __init__(self, routes: Dict[str, object]):
        self.routes = routes
        self.headers: Dict[str, str] = {}
        self.calls = []

    def get(self, url, params=None, timeout=None, verify=None):
        self.calls.append((url, dict(params or {}), timeout, verify))
        route = self.routes.get(url)
        if route is None:
            return FakeResponse(404, {"message": "404 Not Found"})
        if isinstance(route, list):  # 페이지별 응답
            page = int((params or {}).get("page", 1))
            return route[page - 1]
        return route
