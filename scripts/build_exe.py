"""ShipKit.exe 빌드 (PyInstaller, 파일 하나짜리 실행 파일).

사용법 (저장소 루트에서):
    .venv\\Scripts\\pip install -e ".[keyring,build]"
    .venv\\Scripts\\python scripts\\build_exe.py

결과: dist\\<앱이름>.exe  (Python 이 없는 PC 에서도 실행됨)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from shipkit import __app_name__, __tagline__, __version__  # noqa: E402


def write_version_file(path: Path) -> None:
    """exe 의 파일 속성(자세히 탭)에 보이는 이름·버전 정보."""
    nums = ([int(x) for x in __version__.split(".") if x.isdigit()] + [0, 0, 0, 0])[:4]
    ver = ", ".join(str(n) for n in nums)
    text = f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers=({ver}), prodvers=({ver}), mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('041204B0', [
      StringStruct('ProductName', '{__app_name__}'),
      StringStruct('FileDescription', '{__app_name__} - {__tagline__}'),
      StringStruct('ProductVersion', '{__version__}'),
      StringStruct('FileVersion', '{__version__}'),
      StringStruct('OriginalFilename', '{__app_name__}.exe'),
      StringStruct('InternalName', '{__app_name__}')])]),
    VarFileInfo([VarStruct('Translation', [0x0412, 1200])])
  ]
)
"""
    path.write_text(text, encoding="utf-8")


def main() -> int:
    try:
        import PyInstaller.__main__ as pyinstaller
    except ImportError:
        print("PyInstaller 가 필요합니다: .venv\\Scripts\\pip install -e \".[keyring,build]\"")
        return 2

    build_dir = ROOT / "build"
    build_dir.mkdir(exist_ok=True)
    version_file = build_dir / "version_info.txt"
    write_version_file(version_file)

    pyinstaller.run([
        str(ROOT / "src" / "shipkit" / "__main__.py"),
        "--name", __app_name__,
        "--onefile",
        "--console",
        "--clean",
        "--noconfirm",
        "--paths", str(ROOT / "src"),
        "--collect-submodules", "shipkit",
        # 서버 스크립트·설정 예시 템플릿 (importlib.resources 로 읽음)
        "--add-data", f"{ROOT / 'src' / 'shipkit' / 'templates'}{os.pathsep}shipkit/templates",
        # 키링 백엔드(Windows 자격 증명 관리자)는 entry point 로 찾으므로 메타데이터까지 넣는다
        "--collect-submodules", "keyring",
        "--copy-metadata", "keyring",
        "--version-file", str(version_file),
        "--distpath", str(ROOT / "dist"),
        "--workpath", str(build_dir),
        "--specpath", str(build_dir),
    ])
    exe = ROOT / "dist" / f"{__app_name__}.exe"
    print(f"\n완성: {exe} ({exe.stat().st_size / 1024 / 1024:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
