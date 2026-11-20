"""프로젝트 빌드: 배포 화면의 [빌드] 와 명령어 shipkit build. 빌드만 하려고 IDE 를 켜지 않게.

빌드 명령은 프로젝트 설정의 build_command, 없으면 프리셋 기본값(maven 'mvn -DskipTests compile', gradle 'gradlew classes',
vite·vue-cli 'npm run build'). 프로젝트 로컬 경로에서 셸로 실행하고 출력을 한 줄씩 넘긴다. [취소]는 프로세스 나무를 통째로 끝낸다.
"""
from __future__ import annotations

import locale
import os
import queue
import shlex
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from shipkit import idebuild
from shipkit.config import ProjectConfig

DEFAULTS = {"maven": "mvn -DskipTests compile", "gradle": "gradle classes", "vite": "npm run build", "vue-cli": "npm run build"}
POLL = 0.2   # 취소를 확인하는 간격(초)


class BuildError(Exception):
    pass


@dataclass
class BuildResult:
    command: str
    code: int
    elapsed: float
    cancelled: bool = False

    @property
    def ok(self) -> bool:
        return self.code == 0 and not self.cancelled

    def summary(self) -> str:
        secs = f"{self.elapsed:.0f}초"
        if self.cancelled:
            return f"빌드를 취소했습니다 ({secs})"
        return f"빌드 성공 ({secs})" if self.ok else f"빌드 실패 (종료 코드 {self.code}, {secs})"


def default_command(preset: Optional[str], root: Optional[Path] = None) -> str:
    """프리셋의 기본 빌드 명령. gradle 은 저장소에 래퍼(gradlew)가 있으면 그것으로. 빌드가 필요 없는 프리셋은 빈 글."""
    cmd = DEFAULTS.get(preset or "", "")
    if preset == "gradle" and root is not None:
        if os.name == "nt" and (root / "gradlew.bat").is_file():
            cmd = "gradlew.bat classes"
        elif os.name != "nt" and (root / "gradlew").is_file():
            cmd = "./gradlew classes"
    return cmd


def command_for(project: ProjectConfig) -> str:
    """설정의 빌드 명령, 없으면 프리셋 기본값 (그것도 없으면 빈 글)."""
    return (project.build_command or "").strip() or default_command(project.preset, project.root)


# ---------------------------------------------------------------------- 도구 찾기 (PATH 에 없을 때)
# IDE(IntelliJ)로만 빌드하던 PC 에는 mvn·JDK 가 PATH 에 없다. 그런 PC 에서도 되게 흔한 위치를 뒤진다.
_PROGRAM_DIRS = [os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                 os.environ.get("LOCALAPPDATA", ""), os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs"),
                 os.path.join(os.environ.get("LOCALAPPDATA", ""), "JetBrains", "Toolbox", "apps"), "C:\\", r"C:\tools",
                 r"C:\ProgramData\chocolatey\lib"]
TOOL_FILES = {"mvn": ("mvn.cmd", "mvn.bat", "mvn"), "gradle": ("gradle.bat", "gradle"), "npm": ("npm.cmd", "npm")}
WRAPPERS = {"mvn": ("mvnw.cmd", "mvnw"), "gradle": ("gradlew.bat", "gradlew")}


def _program_dirs() -> List[Path]:
    return [Path(d) for d in _PROGRAM_DIRS if d and Path(d).is_dir()]


def _glob_dirs(patterns: Sequence[str]) -> List[Path]:
    """프로그램 폴더들 아래에서 패턴에 맞는 폴더 (최근 버전이 먼저 오게 이름 역순)."""
    found: List[Path] = []
    for base in _program_dirs():
        for pat in patterns:
            found += [p for p in base.glob(pat) if p.is_dir()]
    return sorted(set(found), key=lambda p: str(p).lower(), reverse=True)


def _tool_in(folder: Path, tool: str) -> Optional[Path]:
    for name in TOOL_FILES[tool]:
        f = folder / name
        if f.is_file():
            return f
    return None


def find_tool(tool: str, root: Optional[Path] = None) -> Optional[Path]:
    """mvn / gradle / npm 실행 파일. 순서: PATH → 프로젝트 래퍼(mvnw·gradlew) → MAVEN_HOME·M2_HOME·GRADLE_HOME →
    IntelliJ 에 딸린 Maven → 흔한 설치 폴더(apache-maven-*, gradle-*, nodejs). 못 찾으면 None."""
    on_path = shutil.which(tool)
    if on_path:
        return Path(on_path)
    if root is not None and tool in WRAPPERS:
        for name in WRAPPERS[tool]:
            if (root / name).is_file():
                return root / name
    homes = {"mvn": ("MAVEN_HOME", "M2_HOME"), "gradle": ("GRADLE_HOME",), "npm": ()}[tool]
    for var in homes:
        home = os.environ.get(var, "")
        if home and (hit := _tool_in(Path(home) / "bin", tool)):
            return hit
    folders: List[Path] = []
    if tool == "mvn":
        folders += [d / "plugins" / "maven" / "lib" / "maven3" / "bin"
                    for d in _glob_dirs(("JetBrains/IntelliJ IDEA*", "IntelliJ IDEA*", "IntelliJ IDEA*/ch-*/*"))]
        folders += [d / "bin" for d in _glob_dirs(("apache-maven-*", "Apache/apache-maven-*", "maven/apache-maven-*", "Maven/apache-maven-*"))]
    elif tool == "gradle":
        folders += [d / "bin" for d in _glob_dirs(("gradle-*", "Gradle/gradle-*"))]
    else:
        folders += [Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "nodejs", Path(os.environ.get("APPDATA", "")) / "npm"]
        folders += [d for d in _glob_dirs(("nvm/v*", "nodejs",))]
    for folder in folders:
        if hit := _tool_in(folder, tool):
            return hit
    return None


def find_java_home() -> Optional[Path]:
    """JAVA_HOME 이 없고 PATH 에 java 도 없을 때 쓸 JDK: 흔한 설치 폴더와 IntelliJ 에 딸린 JBR."""
    env = os.environ.get("JAVA_HOME", "")
    if env and (Path(env) / "bin").is_dir():
        return Path(env)
    java = shutil.which("java")
    if java:
        return None   # PATH 의 java 를 그대로 쓴다
    patterns = ("Java/jdk*", "Eclipse Adoptium/jdk*", "Microsoft/jdk*", "Amazon Corretto/jdk*", "Zulu/zulu*", "BellSoft/*jdk*",
                "OpenJDK/jdk*", "jdk*", "JetBrains/IntelliJ IDEA*/jbr", "IntelliJ IDEA*/jbr", "IntelliJ IDEA*/ch-*/*/jbr")
    for d in _glob_dirs(patterns):
        if (d / "bin" / ("java.exe" if os.name == "nt" else "java")).is_file():
            return d
    return None


def resolve_command(cmd: str, root: Path) -> Tuple[str, List[str]]:
    """명령의 첫 낱말이 mvn·gradle·npm 인데 PATH 에 없으면 찾은 실행 파일로 바꾼다. 반환: (명령, 안내 줄들).
    못 찾으면 BuildError (설치하거나 전체 경로를 적으라고)."""
    notes: List[str] = []
    try:
        head = shlex.split(cmd, posix=False)[0].strip('"') if cmd.strip() else ""
    except ValueError:
        head = cmd.split()[0]
    tool = head.lower().removesuffix(".cmd").removesuffix(".bat").removesuffix(".exe")
    if tool in TOOL_FILES and not shutil.which(head):
        found = find_tool(tool, root)
        if found is None:
            hint = {"mvn": "Maven 을 설치해 PATH 에 넣거나 MAVEN_HOME 을 두세요. IntelliJ 가 있으면 그 안의 Maven 을 자동으로 찾는데, "
                           "그것도 없으면 프로젝트 설정의 [빌드 명령]에 전체 경로를 적으세요 "
                           "(예: \"C:\\Program Files\\JetBrains\\IntelliJ IDEA 2024.1\\plugins\\maven\\lib\\maven3\\bin\\mvn.cmd\" -DskipTests compile).",
                    "gradle": "Gradle 을 설치해 PATH 에 넣거나 저장소에 gradlew 래퍼를 두세요.",
                    "npm": "Node.js 를 설치해 PATH 에 넣으세요."}[tool]
            raise BuildError(f"'{head}' 을 찾지 못했습니다 (PATH 에 없음). {hint}")
        cmd = f'"{found}"' + cmd[len(cmd.split(None, 1)[0]):]
        notes.append(f"  ({head} 이 PATH 에 없어 {found} 를 씁니다)")
    return cmd, notes


def build_env() -> Tuple[Dict[str, str], List[str]]:
    """빌드 프로세스의 환경: JAVA_HOME 이 없으면 찾은 JDK 를 넣고 PATH 앞에 그 bin 을 둔다. 반환: (환경, 안내 줄들)."""
    env = dict(os.environ)
    notes: List[str] = []
    if not env.get("JAVA_HOME") and not shutil.which("java"):
        home = find_java_home()
        if home is not None:
            env["JAVA_HOME"] = str(home)
            env["PATH"] = str(home / "bin") + os.pathsep + env.get("PATH", "")
            notes.append(f"  (JAVA_HOME 이 없어 {home} 를 씁니다)")
    return env, notes


def _decode(line: bytes, fallback: str) -> str:
    """한 줄을 글로 (UTF-8 → 안 되면 콘솔 인코딩). 줄 끝은 \\n 으로."""
    try:
        text = line.decode("utf-8")
    except UnicodeDecodeError:
        text = line.decode(fallback, "replace")
    return text.replace("\r\n", "\n")


def _kill(proc: subprocess.Popen) -> None:
    """프로세스 나무를 끝낸다 (셸 → mvn → java 처럼 자식까지)."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
    else:
        try:
            os.killpg(os.getpgid(proc.pid), 15)
        except (ProcessLookupError, PermissionError):
            proc.terminate()


def uses_command(project: ProjectConfig, command: str = "") -> bool:
    """명령 실행인지 (빌드 명령이 적혀 있거나 빌드 방식이 command 이거나 이번 명령을 받았으면). 아니면 IDE 식 빌드."""
    return bool((command or "").strip() or (project.build_command or "").strip() or project.build_mode != "ide")


def describe(project: ProjectConfig, command: str = "") -> str:
    """빌드 창 제목에 보일 글: 실행할 명령 또는 'IDE 식 빌드'."""
    if uses_command(project, command):
        return (command or "").strip() or command_for(project) or "(빌드 명령 없음)"
    return "IDE 식 빌드: javac + WEB-INF/lib (의존성은 Maven 에서)"


def _stream(args, shell: bool, cwd: Path, env: Dict[str, str], output: Callable[[str], None],
            is_cancelled: Optional[Callable[[], bool]]) -> Tuple[int, bool]:
    """프로세스를 돌리며 출력을 한 줄씩 넘긴다. 취소되면 프로세스 나무를 끝낸다. 반환: (종료 코드, 취소됐는지)."""
    try:
        proc = subprocess.Popen(args, cwd=str(cwd), shell=shell, stdin=subprocess.DEVNULL, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                **({"start_new_session": True} if os.name != "nt" else {}))
    except OSError as e:
        raise BuildError(f"빌드 명령을 실행하지 못했습니다: {e}") from e
    fallback = locale.getpreferredencoding(False) or "utf-8"
    lines: "queue.Queue[Optional[bytes]]" = queue.Queue()

    def reader() -> None:
        assert proc.stdout is not None
        for raw in iter(proc.stdout.readline, b""):
            lines.put(raw)
        lines.put(None)

    threading.Thread(target=reader, daemon=True).start()
    cancelled = False
    while True:
        try:
            item = lines.get(timeout=POLL)
        except queue.Empty:
            if not cancelled and is_cancelled is not None and is_cancelled():
                cancelled = True
                output("\n[취소] 빌드를 멈춥니다…\n")
                _kill(proc)
            continue
        if item is None:
            break
        output(_decode(item, fallback))
    return proc.wait(), cancelled


def maven_classpath(project: ProjectConfig, output: Callable[[str], None], env: Dict[str, str],
                    is_cancelled: Optional[Callable[[], bool]] = None) -> List[str]:
    """mvn dependency:build-classpath 로 의존성 jar 경로 목록을 받는다 (IDE 식 빌드용)."""
    target = project.root / "target"
    target.mkdir(parents=True, exist_ok=True)
    out_file = target / "shipkit-mdep.txt"
    cmd, notes = resolve_command(f'mvn -q dependency:build-classpath "-Dmdep.outputFile={out_file}"', project.root)
    output("$ mvn -q dependency:build-classpath\n")
    for note in notes:
        output(note + "\n")
    code, cancelled = _stream(cmd, True, project.root, env, output, is_cancelled)
    if cancelled:
        raise BuildError("취소했습니다.")
    if code != 0 or not out_file.is_file():
        raise BuildError("의존성 경로를 가져오지 못했습니다 (mvn dependency:build-classpath 실패). 위 Maven 출력을 보세요. "
                         "저장소(repository)에 접속할 수 없거나 pom.xml 의 의존성을 못 받는 경우가 많습니다.")
    raw = out_file.read_text(encoding="utf-8", errors="replace").strip()
    return [e for e in raw.split(os.pathsep) if e.strip()]


def run_build(project: ProjectConfig, output: Callable[[str], None], is_cancelled: Optional[Callable[[], bool]] = None,
              command: str = "") -> BuildResult:
    """빌드. 빌드 명령이 있으면(설정·이번 명령) 그것을 프로젝트 폴더에서 실행하고, 없고 빌드 방식이 IDE 식이면 javac 로
    직접 컴파일한다 (idebuild). 출력은 output 으로 한 줄씩. 명령도 없고 IDE 식도 아니면 BuildError."""
    if not project.root.is_dir():
        raise BuildError(f"프로젝트 로컬 경로가 없습니다: {project.root}")
    env, env_notes = build_env()
    start = time.monotonic()
    if not uses_command(project, command):
        java_home = Path(env["JAVA_HOME"]) if env.get("JAVA_HOME") else None
        try:
            code, cancelled, label = idebuild.run(
                project, output, lambda args, cwd: _stream(args, False, cwd, env, output, is_cancelled),
                lambda: maven_classpath(project, output, env, is_cancelled), java_home, is_cancelled)
        except idebuild.IdeBuildError as e:
            raise BuildError(str(e)) from e
        for note in env_notes:
            output(note + "\n")
        return BuildResult(label, code, time.monotonic() - start, cancelled)
    cmd = (command or "").strip() or command_for(project)
    if not cmd:
        raise BuildError("빌드 명령이 없습니다. 프로젝트 설정의 [빌드 명령]에 적으세요 "
                         "(maven·gradle·vite·vue-cli 프리셋은 기본값이 있습니다).")
    shown = cmd
    cmd, notes = resolve_command(cmd, project.root)
    output(f"$ {shown}\n")
    output(f"  (폴더: {project.root})\n")
    for note in notes + env_notes:
        output(note + "\n")
    code, cancelled = _stream(cmd, True, project.root, env, output, is_cancelled)
    return BuildResult(shown, code, time.monotonic() - start, cancelled)
