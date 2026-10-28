# ShipKit

**Safe Deployment & Rollback Tool**

ShipKit is a lightweight deployment tool that collects changed files from a GitLab MR, git diff, or file list, maps them to server paths, and applies them safely with automatic backup and one-command rollback.

ShipKit은 GitLab MR·git diff·파일 목록에서 바뀐 파일만 모아 서버 경로에 맞게 정리하고, 자동 백업 후 반영하며, 문제가 생기면 명령 한 줄로 되돌리는 가벼운 배포 도구입니다.

> 현재 버전(0.1)은 **이미 빌드된 Java 웹 프로젝트**(Maven/Gradle, exploded WAR)를 중심으로 동작합니다. 컴파일은 하지 않으니 IDE나 `mvn compile`로 빌드한 뒤 실행하세요. 소스가 클래스보다 최신이면 "빌드 누락 의심" 경고를 냅니다.

## 주요 기능

- **변경 파일 수집**: GitLab MR(여러 개 합치기 가능), 로컬 `git diff` 범위, 파일 목록, 경로 직접 입력
- **서버 경로 매핑**: `X.java` → `X.class` + 내부 클래스(`X$*.class`), 리소스 → `WEB-INF/classes`, webapp → 루트. 프리셋(`maven`/`gradle`), 멀티모듈, 직접 규칙 지원
- **삭제·이름변경 처리**: 서버에서 지울 파일을 `delete.txt`로 만들고, 원복 시 되살립니다.
- **안전장치**: 실패 항목이 있으면 패키지를 만들지 않습니다. 빌드 누락, 커밋되지 않은 로컬 수정, 브랜치 불일치, pull 누락을 경고합니다.
- **서버 스크립트 `shipkit.sh`**: `check` → `backup` → `apply` → `rollback`. 줄바꿈은 항상 LF이고, 한 번 만든 백업은 덮어쓰지 않습니다.
- **보고서 `report.md`**: MR 정보, 파일별 처리 결과, 서버 반영 절차
- **보안**: 토큰은 환경변수나 OS 키링(Windows 자격 증명 관리자)에만 둡니다. 설정 파일에 넣으면 거부합니다. SSL 검증은 기본으로 켜져 있고, 모든 요청에 타임아웃이 있습니다.

## 설치

Python 3.9 이상이 필요합니다.

```bash
python -m venv .venv
.venv\Scripts\pip install -e ".[keyring]"
```

설치하면 `.venv\Scripts\shipkit.exe`가 생깁니다. 이 폴더를 PATH에 추가하면 어디서든 `shipkit`으로 실행할 수 있습니다.

## 사용법

```bash
shipkit init                      # 빈 설정 파일을 만들어 직접 작성
#   또는 shipkit import-legacy    # 기존 배포 툴(compiled-java-deployer)의 프로젝트 설정 가져오기 (토큰·비밀번호 제외)
shipkit token set                 # GitLab 토큰을 OS 키링에 저장 (입력 내용은 화면에 표시되지 않음)
shipkit check                     # 설정·경로·빌드 폴더·GitLab 연결 점검

shipkit mr -p erp                       # 최근 머지된 MR 목록 (--state opened|closed|all, --search)
shipkit package -p erp --mr 128 131     # MR 기준 패키지
shipkit package -p erp --git v1.2..HEAD # 로컬 git 범위 기준 (토큰 불필요)
shipkit package -p erp --files 목록.txt # 파일 목록 기준
shipkit package -p erp src/main/webapp/a.jsp  # 경로 직접 나열
```

옵션:
- `--archive tar|zip`: 패키지를 압축합니다. 서버용으로는 `tar`(.tar.gz)를 권장합니다. 실행 권한이 들어 있어 `chmod` 없이 바로 실행할 수 있습니다.
- `--dry-run`: 패키지를 만들지 않고 결과만 출력합니다.
- `--strict`: 경고도 실패로 처리합니다.
- `--force`: 실패한 항목을 빼고 나머지로 패키지를 만듭니다.

`--files` 목록에는 `git diff --name-status` 출력을 그대로 붙여넣어도 됩니다(`D<TAB>경로`는 삭제, `R100<TAB>이전<TAB>새`는 이름변경).

**실패 항목이 하나라도 있으면 기본적으로 패키지를 만들지 않습니다.** 빌드 누락 같은 문제가 있는 채로 일부만 배포되는 사고를 막기 위해서입니다.

다음 경우에는 경고를 띄웁니다(`--strict`를 붙이면 경고도 실패로 처리).
- 소스가 클래스 파일보다 최신일 때: 빌드 누락이 의심됩니다.
- 배포 파일에 커밋되지 않은 로컬 수정이 있을 때: 테스트용 설정 등이 운영에 섞일 수 있습니다.
- `--git`의 범위 끝과 지금 브랜치(HEAD)가 다를 때: 복사되는 파일은 지금 작업 폴더의 파일입니다.
- `--mr`인데 로컬 저장소에 그 MR 커밋이 없을 때: git pull이 필요합니다.

## 설정 파일

다음 순서로 찾습니다.

1. `--config` 인자
2. 환경변수 `SHIPKIT_CONFIG`
3. 현재 폴더의 `shipkit.yaml`
4. `%APPDATA%\shipkit\shipkit.yaml`

`init`과 `import-legacy`는 기본으로 4번 위치에 만들기 때문에, 어느 폴더에서 실행해도 같은 설정을 씁니다. 예시는 [shipkit.example.yaml](src/shipkit/templates/shipkit.example.yaml)에 있습니다.

```yaml
gitlab:
  url: https://gitlab.example.com
  verify_ssl: true

defaults:
  package_name: "%Y-%m-%d-%H%M"     # 날짜 폴더 이름 -> 2026-10-27-1530

projects:
  erp:
    root: C:/workspace/erp                  # 로컬 git 저장소 최상위 폴더
    output_dir: D:/deploy/erp               # 이 아래에 날짜 폴더가 생김
    server_path: /app/tomcat/webapps/ROOT   # 서버의 WEB-INF 바로 위 폴더
    gitlab_project: 123
```

Windows 경로는 `/`로 적는 것을 권장합니다.

## 패키지 구조와 서버 반영

`output_dir` 아래에 날짜 폴더가 생깁니다. 이름 형식은 `package_name`으로 바꿀 수 있고, 같은 이름이 이미 있으면 뒤에 `_2`를 붙입니다.

```
D:/deploy/erp/2026-10-27-1530/      ← 패키지 폴더
├─ files/          서버 배포 경로 기준 그대로 (WEB-INF/classes/..., *.jsp, css, js)
├─ shipkit.sh      서버용 스크립트 (LF)
├─ manifest.txt    반영할 파일 목록
├─ delete.txt      서버에서 삭제할 파일 목록
└─ report.md       배포 보고서
```

**패키지 폴더는 날짜 폴더 자체입니다.** 이 폴더를 통째로(또는 .tar.gz로) 서버에 올리되, 서버 배포 경로 **밖**(예: `~/deploy/2026-10-27-1530`)에 두세요. 그 폴더로 이동해 다음을 실행합니다.

```bash
chmod +x shipkit.sh       # 실행 권한 부여 (tar.gz로 올렸다면 필요 없음)
./shipkit.sh check        # 신규/변경/동일/삭제예정 확인 (서버 변경 없음)
./shipkit.sh backup       # 반영·삭제될 기존 파일을 backup/ 에 보관
./shipkit.sh apply        # 반영 + 삭제 (backup 없이는 실행되지 않음)
./shipkit.sh rollback     # backup 기준으로 원복 (새로 추가된 파일은 제거)
```

`chmod` 없이 `bash shipkit.sh check`처럼 실행해도 됩니다.

배포 파일은 `files/` 아래에 따로 들어 있습니다. 그래서 WinSCP 등으로 직접 옮길 때도 `files/` 안의 내용만 `server_path`로 복사하면 되고, 스크립트나 보고서가 웹 루트에 섞이지 않습니다.

대상 경로는 설정의 `server_path`가 기본값이고, 두 번째 인자로 바꿀 수 있습니다(`./shipkit.sh apply /app/other/ROOT`). `apply`는 파일 수정 시각을 '지금'으로 두어 WAS가 변경을 감지하게 합니다. WAS 재시작은 하지 않습니다.

## 종료 코드

| 코드 | 의미 |
|---|---|
| 0 | 성공 |
| 1 | 처리 실패, `--strict` 경고, GitLab/git 오류 |
| 2 | 설정·사용법 오류 |

Jenkins 등에서 그대로 쓸 수 있습니다.

## 개발

```bash
.venv\Scripts\pip install -e ".[keyring,dev]"
.venv\Scripts\python -m pytest
```

테스트는 bash(Windows에서는 Git Bash)로 `shipkit.sh`의 check, backup, apply, rollback을 실제로 실행해 검증합니다. bash가 없으면 해당 테스트는 건너뜁니다.

## 알려진 제한

- 멀티모듈 프로젝트에서 다른 모듈의 클래스가 `WEB-INF/lib/*.jar`로 들어가는 구조는 지원하지 않습니다(jar를 다시 빌드해야 함).
- 서버 셸은 bash를 가정합니다.
- 로컬 경로(`root`)는 git 저장소 최상위 폴더여야 합니다.
