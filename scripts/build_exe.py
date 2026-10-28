"""exe 빌드 (PyInstaller, 파일 하나짜리 실행 파일 2개).

  dist\\<앱이름>.exe        GUI - 더블클릭해서 쓰는 화면 버전 (팀원 배포용)
  dist\\<명령어>-cli.exe    CLI - 명령어 버전 (Jenkins 등 자동화용)

Windows 는 파일 이름의 대소문자를 구분하지 않아서 ShipKit.exe 와 shipkit.exe 가 충돌하므로 CLI 는 -cli 를 붙인다.

사용법 (저장소 루트에서):
    .venv\\Scripts\\pip install -e ".[keyring,gui,build]"
    .venv\\Scripts\\python scripts\\build_exe.py          # 둘 다
    .venv\\Scripts\\python scripts\\build_exe.py gui      # GUI 만
    .venv\\Scripts\\python scripts\\build_exe.py cli      # CLI 만
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from shipkit import CLI_NAME, __app_name__, __tagline__, __version__  # noqa: E402


def write_version_file(path: Path, exe_name: str, description: str) -> None:
    """exe 의 파일 속성(자세히 탭)에 보이는 이름·버전 정보."""
    nums = ([int(x) for x in __version__.split(".") if x.isdigit()] + [0, 0, 0, 0])[:4]
    ver = ", ".join(str(n) for n in nums)
    text = f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers=({ver}), prodvers=({ver}), mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('041204B0', [
      StringStruct('ProductName', '{__app_name__}'),
      StringStruct('FileDescription', '{description}'),
      StringStruct('ProductVersion', '{__version__}'),
      StringStruct('FileVersion', '{__version__}'),
      StringStruct('OriginalFilename', '{exe_name}.exe'),
      StringStruct('InternalName', '{exe_name}')])]),
    VarFileInfo([VarStruct('Translation', [0x0412, 1200])])
  ]
)
"""
    path.write_text(text, encoding="utf-8")


def make_icon(path: Path):
    """로고 svg 를 exe 아이콘(.ico)으로 만든다. 실패하면 기본 아이콘을 쓴다."""
    try:
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QGuiApplication, QImage, QPainter
        from PySide6.QtSvg import QSvgRenderer
        app = QGuiApplication.instance() or QGuiApplication(["build"])  # noqa: F841
        image = QImage(256, 256, QImage.Format.Format_ARGB32)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        QSvgRenderer(str(ROOT / "src" / "shipkit" / "gui" / "assets" / "logo.svg")).render(painter)
        painter.end()
        path.parent.mkdir(parents=True, exist_ok=True)
        if image.save(str(path), "ICO"):
            return path
    except Exception as e:  # noqa: BLE001
        print(f"아이콘을 만들지 못했습니다 (기본 아이콘 사용): {e}")
    return None


def build(pyinstaller, kind: str) -> Path:
    gui = kind == "gui"
    name = __app_name__ if gui else f"{CLI_NAME}-cli"
    entry = ROOT / "src" / "shipkit" / ("gui" if gui else "") / "__main__.py"
    work = ROOT / "build" / kind
    work.mkdir(parents=True, exist_ok=True)
    version_file = work / "version_info.txt"
    write_version_file(version_file, name, f"{__app_name__} - {__tagline__}" + ("" if gui else " (CLI)"))

    args = [
        str(entry),
        "--name", name,
        "--onefile",
        "--windowed" if gui else "--console",
        "--clean",
        "--noconfirm",
        "--paths", str(ROOT / "src"),
        # 함수 안에서 늦게 import 하는 모듈도 빠지지 않게 (CLI 는 GUI 모듈 제외)
        *(["--collect-submodules", "shipkit"] if gui else ["--hidden-import", "shipkit.interactive"]),
        # 서버 스크립트·설정 예시 템플릿 (importlib.resources 로 읽음)
        "--add-data", f"{ROOT / 'src' / 'shipkit' / 'templates'}{os.pathsep}shipkit/templates",
        # 키링 백엔드(Windows 자격 증명 관리자)는 entry point 로 찾으므로 메타데이터까지 넣는다
        "--collect-submodules", "keyring",
        "--copy-metadata", "keyring",
        "--version-file", str(version_file),
        "--distpath", str(ROOT / "dist"),
        "--workpath", str(work),
        "--specpath", str(work),
    ]
    if gui:
        # 화면 아이콘(svg)과 exe 아이콘
        args += ["--add-data", f"{ROOT / 'src' / 'shipkit' / 'gui' / 'assets'}{os.pathsep}shipkit/gui/assets"]
        icon = make_icon(work / "ShipKit.ico")
        if icon:
            args += ["--icon", str(icon)]
    else:
        args += ["--exclude-module", "PySide6", "--exclude-module", "shipkit.gui"]
    pyinstaller.run(args)
    exe = ROOT / "dist" / f"{name}.exe"
    print(f"\n완성: {exe} ({exe.stat().st_size / 1024 / 1024:.1f} MB)")
    return exe


def main(argv: list) -> int:
    try:
        import PyInstaller.__main__ as pyinstaller
    except ImportError:
        print("PyInstaller 가 필요합니다: .venv\\Scripts\\pip install -e \".[keyring,gui,build]\"")
        return 2
    kinds = argv or ["gui", "cli"]
    unknown = [k for k in kinds if k not in ("gui", "cli")]
    if unknown:
        print(f"알 수 없는 대상: {unknown} (gui 또는 cli)")
        return 2
    built = [build(pyinstaller, k) for k in kinds]
    print("\n" + "\n".join(f"  {p}" for p in built))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
