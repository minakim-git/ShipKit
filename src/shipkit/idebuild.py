"""IDE 식 빌드: IntelliJ 가 하는 것처럼 javac 로 직접 컴파일한다 (Maven 프로젝트).

회사 프로젝트들은 pom.xml 에 없는 jar 가 src/main/webapp/WEB-INF/lib 에 있고 IntelliJ 라이브러리로만 걸려 있어
'mvn compile' 은 실패한다. 그래서 Maven 은 의존성 경로를 알아내는 데만 쓰고(mvn dependency:build-classpath, pom.xml 이
바뀔 때만 다시), 그 경로 + WEB-INF/lib/*.jar + target/classes 를 classpath 로 javac 를 돌린 뒤 src/main/resources 를
target/classes 로 복사한다. 컴파일 옵션(source/target/release, 인코딩, 출력 폴더)은 pom.xml 에서 읽는다.
"""
from __future__ import annotations

import os
import shutil
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

from shipkit.config import ProjectConfig

CLASSPATH_CACHE = "shipkit-classpath.txt"    # target/ 아래. 첫 줄에 pom.xml 의 수정 시각·크기
LIB_DIR = "src/main/webapp/WEB-INF/lib"
JAVA_ENCODINGS = {"utf-8": "UTF-8", "utf-8-sig": "UTF-8", "cp949": "MS949", "euc-kr": "EUC-KR", "euckr": "EUC-KR", "ms949": "MS949"}


class IdeBuildError(Exception):
    pass


@dataclass
class PomInfo:
    source: str = ""           # maven.compiler.source 또는 compiler 플러그인의 <source>
    target: str = ""
    release: str = ""
    encoding: str = ""         # project.build.sourceEncoding
    output_dir: str = "target/classes"
    source_dir: str = "src/main/java"
    resources: List[str] = field(default_factory=lambda: ["src/main/resources"])
    modules: List[str] = field(default_factory=list)
    packaging: str = "jar"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(el: ET.Element, name: str) -> Optional[ET.Element]:
    return next((c for c in el if _local(c.tag) == name), None)


def _children(el: Optional[ET.Element]) -> List[ET.Element]:
    return list(el) if el is not None else []


def _text(el: Optional[ET.Element], name: str) -> str:
    c = _child(el, name) if el is not None else None
    return (c.text or "").strip() if c is not None else ""


def read_pom(pom: Path) -> PomInfo:
    """pom.xml 에서 컴파일에 필요한 것만 읽는다 (프로퍼티 치환은 ${...} 가 프로퍼티면 한 단계만)."""
    try:
        root = ET.parse(pom).getroot()
    except (ET.ParseError, OSError) as e:
        raise IdeBuildError(f"pom.xml 을 읽지 못했습니다: {e}") from e
    info = PomInfo()
    props = {_local(p.tag): (p.text or "").strip() for p in _children(_child(root, "properties"))}

    def prop(value: str) -> str:
        if value.startswith("${") and value.endswith("}"):
            return props.get(value[2:-1], "")
        return value

    info.packaging = _text(root, "packaging") or "jar"
    info.source = prop(props.get("maven.compiler.source", ""))
    info.target = prop(props.get("maven.compiler.target", ""))
    info.release = prop(props.get("maven.compiler.release", ""))
    info.encoding = prop(props.get("project.build.sourceEncoding", ""))
    info.modules = [(m.text or "").strip() for m in _children(_child(root, "modules")) if (m.text or "").strip()]
    build = _child(root, "build")
    if build is not None:
        if _text(build, "outputDirectory"):
            info.output_dir = prop(_text(build, "outputDirectory"))
        if _text(build, "sourceDirectory"):
            info.source_dir = prop(_text(build, "sourceDirectory"))
        res = _child(build, "resources")
        if res is not None:
            dirs = [prop(_text(r, "directory")) for r in res if _local(r.tag) == "resource" and _text(r, "directory")]
            if dirs:
                info.resources = dirs
        for plugin in _children(_child(build, "plugins")):
            if _text(plugin, "artifactId") == "maven-compiler-plugin":
                conf = _child(plugin, "configuration")
                if conf is not None:
                    info.source = prop(_text(conf, "source")) or info.source
                    info.target = prop(_text(conf, "target")) or info.target
                    info.release = prop(_text(conf, "release")) or info.release
                    info.encoding = prop(_text(conf, "encoding")) or info.encoding
    return info


def java_encoding(project: ProjectConfig, info: PomInfo) -> str:
    """javac -encoding 값: pom 의 sourceEncoding, 없으면 프로젝트의 소스 인코딩 첫 번째."""
    raw = info.encoding or (project.source_encodings[0] if project.source_encodings else "utf-8")
    return JAVA_ENCODINGS.get(raw.lower(), raw)


def javac_path(java_home: Optional[Path]) -> Optional[Path]:
    name = "javac.exe" if os.name == "nt" else "javac"
    candidates = []
    if java_home is not None:
        candidates.append(java_home / "bin" / name)
    on_path = shutil.which("javac")
    if on_path:
        candidates.append(Path(on_path))
    return next((c for c in candidates if c.is_file()), None)


def _pom_stamp(pom: Path) -> str:
    st = pom.stat()
    return f"# pom {int(st.st_mtime)} {st.st_size}"


def cached_classpath(root: Path, pom: Path) -> Optional[List[str]]:
    cache = root / "target" / CLASSPATH_CACHE
    if not cache.is_file():
        return None
    lines = cache.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0] != _pom_stamp(pom):
        return None
    return [ln for ln in lines[1:] if ln.strip()]


def save_classpath(root: Path, pom: Path, entries: List[str]) -> None:
    cache = root / "target" / CLASSPATH_CACHE
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text("\n".join([_pom_stamp(pom)] + entries) + "\n", encoding="utf-8")


def resource_copy(root: Path, dirs: List[str], out_dir: Path) -> int:
    """리소스 폴더들을 출력 폴더로 복사한다 (IntelliJ 처럼. Maven 의 필터링은 하지 않는다). 반환: 복사한 파일 수."""
    n = 0
    for d in dirs:
        base = root / d
        if not base.is_dir():
            continue
        for f in base.rglob("*"):
            if f.is_file():
                dest = out_dir / f.relative_to(base)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, dest)
                n += 1
    return n


def sources_of(root: Path, info: PomInfo) -> List[Path]:
    src = root / info.source_dir
    return sorted(src.rglob("*.java")) if src.is_dir() else []


def lib_jars(root: Path) -> List[Path]:
    lib = root / LIB_DIR
    return sorted(lib.glob("*.jar")) if lib.is_dir() else []


def argfile_line(path: Path) -> str:
    """javac @파일의 한 줄: 따옴표로 감싸고 역슬래시는 슬래시로."""
    return '"' + path.as_posix() + '"'


def run(project: ProjectConfig, output: Callable[[str], None], run_process, maven_classpath, java_home: Optional[Path],
        is_cancelled: Optional[Callable[[], bool]] = None):
    """IDE 식 빌드. run_process(args, cwd, env) -> (종료 코드, 취소됐는지), maven_classpath() -> 경로 목록 (build 모듈이 준다).
    반환: (종료 코드, 취소됐는지, 설명)."""
    root = project.root
    pom = root / "pom.xml"
    if not pom.is_file():
        raise IdeBuildError(f"IDE 식 빌드는 pom.xml 이 있는 Maven 프로젝트에서만 됩니다 ({root} 에 pom.xml 없음). "
                            "프로젝트 설정의 빌드 방식을 '명령 실행' 으로 바꾸고 빌드 명령을 적으세요.")
    info = read_pom(pom)
    if info.modules:
        raise IdeBuildError(f"멀티모듈 프로젝트({', '.join(info.modules)})는 IDE 식 빌드가 지원하지 않습니다. "
                            "빌드 방식을 '명령 실행' 으로 바꾸고 빌드 명령(예: mvn -DskipTests compile)을 적으세요.")
    javac = javac_path(java_home)
    if javac is None:
        raise IdeBuildError("javac 를 찾지 못했습니다. JDK(JRE 말고)를 설치하고 JAVA_HOME 을 두거나 PATH 에 넣으세요. "
                            "IntelliJ 가 있으면 그 안의 JBR 도 찾아봅니다.")
    sources = sources_of(root, info)
    if not sources:
        raise IdeBuildError(f"자바 소스가 없습니다: {root / info.source_dir}")
    out_dir = root / info.output_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    output(f"$ IDE 식 빌드: javac + WEB-INF/lib  (폴더: {root})\n")

    cp = cached_classpath(root, pom)
    if cp is None:
        output("  의존성 경로를 Maven 에서 가져옵니다 (pom.xml 이 바뀔 때만 다시)…\n")
        cp = maven_classpath()
        save_classpath(root, pom, cp)
    else:
        output(f"  의존성 경로 {len(cp)}개 (지난번 것, pom.xml 그대로)\n")
    libs = lib_jars(root)
    classpath = [str(out_dir)] + [str(j) for j in libs] + cp
    encoding = java_encoding(project, info)
    args: List[str] = [str(javac), "-d", str(out_dir), "-encoding", encoding, "-nowarn"]
    if info.release:
        args += ["--release", info.release]
    else:
        if info.source:
            args += ["-source", info.source]
        if info.target:
            args += ["-target", info.target]
    args += ["-cp", os.pathsep.join(classpath)]
    argfile = root / "target" / "shipkit-sources.txt"
    argfile.write_text("\n".join(argfile_line(s) for s in sources) + "\n", encoding="utf-8")
    args.append("@" + str(argfile))
    opts = " ".join(args[1:-2]) + " …"      # classpath 값과 @소스 목록은 길어서 뺀다
    output(f"$ javac {opts}  (소스 {len(sources)}개, WEB-INF/lib jar {len(libs)}개, 의존성 {len(cp)}개)\n")
    start = time.monotonic()
    code, cancelled = run_process(args, root)
    if code == 0 and not cancelled:
        n = resource_copy(root, info.resources, out_dir)
        output(f"  리소스 {n}개를 {out_dir} 로 복사했습니다 ({', '.join(info.resources)})\n")
        output(f"  컴파일 {time.monotonic() - start:.0f}초, 결과: {out_dir}\n")
    return code, cancelled, f"javac (소스 {len(sources)}개, WEB-INF/lib jar {len(libs)}개)"
