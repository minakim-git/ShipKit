# ShipKit

**Safe Deployment & Rollback Tool**

ShipKit is a lightweight deployment tool that collects changed files from a GitLab MR, git diff, or file list, maps them to server paths, and applies them safely with automatic backup and one-command rollback.

ShipKit은 GitLab MR·git diff·파일 목록에서 바뀐 파일만 모아 서버 경로에 맞게 정리하고, 자동 백업 후 반영하며, 문제가 생기면 명령 한 줄로 되돌리는 가벼운 배포 도구입니다.

Java, Vue/React, Python, PHP, 정적 사이트 등 언어와 상관없이 쓸 수 있습니다. 빌드(컴파일)는 하지 않으니, 빌드가 필요한 프로젝트는 빌드를 끝낸 뒤 실행하세요.

## 주요 기능

- **변경 파일 수집**: GitLab MR(여러 개 합치기 가능), 로컬 `git diff` 범위, 파일 목록, 경로 직접 입력
- **두 가지 배포 방식**
  - `changes` (변경 파일 매핑): 바뀐 파일만 서버 경로로 매핑합니다. Java는 `X.java` → `X.class` + 내부 클래스(`X$*.class`)로 바꿉니다.
  - `sync` (빌드 결과 동기화): Vue/React처럼 결과 파일 이름이 빌드마다 바뀌는 프로젝트용입니다. 빌드 결과 폴더 전체를 담고, 서버에서 **내용이 다른 파일만** 반영하며, 지정한 폴더의 **옛 빌드 파일을 정리**합니다.
- **프리셋**: `maven`, `gradle`, `vite`, `vue-cli`, `python`, `static` (`shipkit presets`로 확인)
- **삭제·이름변경 처리**: 서버에서 지울 파일을 `delete.txt`로 만들고, 원복 시 되살립니다.
- **안전장치**: 실패 항목이 있으면 패키지를 만들지 않습니다. 빌드 누락, 커밋되지 않은 로컬 수정, 브랜치 불일치, pull 누락을 경고합니다.
- **민감 파일 차단**: `.env`, `*.pem`, `*.key`, `*.jks`, `id_rsa` 같은 파일은 기본으로 패키지에 넣지 않습니다(`allow`로 예외 지정).
- **알림**: 의존성 파일(`pom.xml`, `requirements.txt`, `package.json` 등), DB 마이그레이션, SQL 파일이 바뀌면 "패키지 외에 따로 할 일"로 알려줍니다.
- **서버 스크립트 `shipkit.sh`**: `check` → `backup` → `apply` → `rollback`, `history`. 줄바꿈은 항상 LF이고, 한 번 만든 백업은 덮어쓰지 않으며, 서버와 같은 파일은 건드리지 않습니다.
- **반영 전·후 명령(hooks)**: WAS 재시작, `pip install` 같은 명령을 설정해 두면 apply·rollback 때 확인을 받고 실행합니다. 특정 파일이 바뀐 경우에만 넣을 수도 있습니다(`when`).
- **서버 안전장치**: 체크섬으로 업로드 중 손상 검증, 같은 대상에 대한 동시 배포 잠금, 서버별 배포 이력, 새로 만든 파일·폴더의 소유자·권한 지정
- **보고서 `report.md`**: MR 정보, 파일별 처리 결과, 알림, 서버 반영 절차
- **보안**: 토큰은 환경변수나 OS 키링(Windows 자격 증명 관리자)에만 둡니다. 설정 파일에 넣으면 거부합니다. SSL 검증은 기본으로 켜져 있고, 모든 요청에 타임아웃이 있습니다.

## 프리셋

| 프리셋 | 방식 | 대상 | 동작 |
|---|---|---|---|
| `maven` | changes | Java 웹 (Maven, exploded WAR) | `.java` → `target/classes`의 `.class`, 리소스 → `WEB-INF/classes`, webapp → 루트 |
| `gradle` | changes | Java 웹 (Gradle) | `maven`과 같고, 클래스 위치만 `build/classes/java/main` |
| `python` | changes | Django, Flask, FastAPI 등 | 바뀐 소스를 그대로 반영. `__pycache__`, 가상환경, 테스트는 제외 |
| `static` | changes | 정적 사이트, PHP, Node 서버 등 | 바뀐 파일을 그대로 반영. `node_modules` 제외 |
| `vite` | sync | Vue 3, React 등 (Vite) | `dist/` 전체 동기화, `assets/`의 옛 파일 정리 |
| `vue-cli` | sync | Vue 2/3 (Vue CLI) | `dist/` 전체 동기화, `js/`, `css/`, `img/`, `fonts/`, `media/`의 옛 파일 정리 |

별칭: `vue-vite`, `react-vite`, `vue` → `vite` / `php`, `node` → `static`

프리셋이 맞지 않으면 `rules`(changes 방식)나 `build_dir`·`prune`(sync 방식)으로 직접 지정할 수 있습니다.

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
shipkit presets                   # 사용 가능한 프리셋 목록

shipkit mr -p erp                       # 최근 머지된 MR 목록 (--state opened|closed|all, --search)
shipkit package -p erp --mr 128 131     # MR 기준 패키지
shipkit package -p erp --git v1.2..HEAD # 로컬 git 범위 기준 (토큰 불필요)
shipkit package -p erp --files 목록.txt # 파일 목록 기준
shipkit package -p erp src/main/webapp/a.jsp  # 경로 직접 나열

shipkit package -p web                  # sync 방식(vite 등)은 변경 출처 없이 빌드 결과 전체로 패키지
shipkit package -p web --mr 42          # MR 을 같이 주면 보고서에 소스 변경 내역이 남고 빌드 누락도 검사
```

옵션:
- `--archive tar|zip`: 패키지를 압축합니다. 서버용으로는 `tar`(.tar.gz)를 권장합니다. 실행 권한이 들어 있어 `chmod` 없이 바로 실행할 수 있습니다.
- `--dry-run`: 패키지를 만들지 않고 결과만 출력합니다.
- `--strict`: 경고도 실패로 처리합니다.
- `--force`: 실패한 항목을 빼고 나머지로 패키지를 만듭니다.

`--files` 목록에는 `git diff --name-status` 출력을 그대로 붙여넣어도 됩니다(`D<TAB>경로`는 삭제, `R100<TAB>이전<TAB>새`는 이름변경).

**실패 항목이 하나라도 있으면 기본적으로 패키지를 만들지 않습니다.** 빌드 누락 같은 문제가 있는 채로 일부만 배포되는 사고를 막기 위해서입니다.

다음 경우에는 경고를 띄웁니다(`--strict`를 붙이면 경고도 실패로 처리).
- 소스가 클래스 파일(또는 빌드 결과)보다 최신일 때: 빌드 누락이 의심됩니다.
- 배포 파일에 커밋되지 않은 로컬 수정이 있을 때: 테스트용 설정 등이 운영에 섞일 수 있습니다.
- `--git`의 범위 끝과 지금 브랜치(HEAD)가 다를 때: 복사되는 파일은 지금 작업 폴더의 파일입니다.
- `--mr`인데 로컬 저장소에 그 MR 커밋이 없을 때: git pull이 필요합니다.

## 설정 파일

다음 순서로 찾습니다.

1. `--config` 인자
2. 환경변수 `SHIPKIT_CONFIG`
3. 현재 폴더의 `shipkit.yaml`
4. `%APPDATA%\shipkit\shipkit.yaml`

`init`과 `import-legacy`는 기본으로 4번 위치에 만들기 때문에, 어느 폴더에서 실행해도 같은 설정을 씁니다. 전체 예시는 [config.example.yaml](src/shipkit/templates/config.example.yaml)에 있습니다.

```yaml
gitlab:
  url: https://gitlab.example.com
  verify_ssl: true

defaults:
  package_name: "%Y-%m-%d-%H%M"     # 날짜 폴더 이름 -> 2026-10-27-1530

projects:
  erp:                                      # Java 웹
    preset: maven
    root: C:/workspace/erp                  # 로컬 git 저장소 최상위 폴더
    output_dir: D:/deploy/erp               # 이 아래에 날짜 폴더가 생김
    server_path: /app/tomcat/webapps/ROOT   # 서버의 WEB-INF 바로 위 폴더
    gitlab_project: 123

  web:                                      # Vue (Vite)
    preset: vite
    root: C:/workspace/web
    output_dir: D:/deploy/web
    server_path: /var/www/web               # dist/ 의 내용이 들어갈 서버 폴더
    # prune: [assets]                       # 옛 빌드 파일을 정리할 서버 폴더 (프리셋 기본값)

  api:                                      # Python
    preset: python
    root: C:/workspace/api
    output_dir: D:/deploy/api
    server_path: /srv/api
```

Windows 경로는 `/`로 적는 것을 권장합니다. 프리셋을 생략하면 `maven`으로 동작합니다(0.1 설정 호환).

공통 옵션:
- `exclude`: 배포에서 뺄 파일(glob). 프리셋의 기본 제외 목록에 추가됩니다.
- `notices`: `{pattern, message}` 목록. 해당 파일이 바뀌면 알림을 띄웁니다.
- `allow` / `deny`: 민감 파일 차단의 예외 / 추가 대상
- `hooks`: 서버에서 실행할 명령 (아래 참고)
- `server`: 새로 만든 파일·폴더의 소유자·권한, 배포 이력 파일 위치 (아래 참고)

```yaml
    hooks:
      pre_apply:  ["sudo systemctl stop tomcat"]        # 실패하면 apply 를 중단 (서버 파일은 그대로)
      post_apply:
        - "sudo systemctl start tomcat"
        - { run: "pip install -r requirements.txt", when: "requirements*.txt" }  # 이 파일이 바뀐 경우에만
      post_rollback: ["sudo systemctl restart tomcat"]
    server:
      owner: tomcat:tomcat      # 새로 만든 파일·폴더 소유자 ("inherit" = 상위 폴더와 같게)
      file_mode: "644"          # 새로 만든 파일 권한
      dir_mode: "755"           # 새로 만든 폴더 권한
      history_file: ~/.shipkit/history.log
```

기존 파일을 덮어쓸 때는 원래 소유자·권한이 그대로 유지되므로, `server` 설정은 새로 생기는 파일·폴더에만 적용됩니다. `chown`은 권한이 있을 때만 성공하고, 실패하면 경고만 띄웁니다.

## 패키지 구조와 서버 반영

`output_dir` 아래에 날짜 폴더가 생깁니다. 이름 형식은 `package_name`으로 바꿀 수 있고, 같은 이름이 이미 있으면 뒤에 `_2`를 붙입니다.

```
D:/deploy/erp/2026-10-27-1530/      ← 패키지 폴더
├─ files/          서버 배포 경로 기준 그대로 (WEB-INF/classes/..., *.jsp, css, js)
├─ shipkit.sh      서버용 스크립트 (LF)
├─ manifest.txt    반영할 파일 목록
├─ delete.txt      서버에서 삭제할 파일 목록
├─ prune.txt       (sync 방식) 옛 빌드 파일을 정리할 서버 폴더 목록
├─ hooks/          반영 전·후에 서버에서 실행할 명령 (설정한 경우만)
├─ checksums.sha256  패키지 파일 검증용
└─ report.md       배포 보고서
```

**패키지 폴더는 날짜 폴더 자체입니다.** 이 폴더를 통째로(또는 .tar.gz로) 서버에 올리되, 서버 배포 경로 **밖**(예: `~/deploy/2026-10-27-1530`)에 두세요. 그 폴더로 이동해 다음을 실행합니다.

```bash
chmod +x shipkit.sh       # 실행 권한 부여 (tar.gz로 올렸다면 필요 없음)
./shipkit.sh check        # 신규/변경/동일/삭제예정/정리예정 확인 (서버 변경 없음)
./shipkit.sh backup       # 바뀌거나 지워질 기존 파일을 backup/ 에 보관
./shipkit.sh apply        # 반영 + 삭제 + 정리 (backup 없이는 실행되지 않음)
./shipkit.sh rollback     # backup 기준으로 원복 (새로 추가된 파일은 제거)
./shipkit.sh history      # 이 서버의 배포 이력 (최근 20건)
```

`chmod` 없이 `bash shipkit.sh check`처럼 실행해도 됩니다.

옵션:
- `-y`: 반영 전·후 명령을 묻지 않고 실행합니다(Jenkins 등 자동화용). 터미널이 아닌 곳에서 `-y` 없이 실행하면 명령을 실행하지 않고, `pre_apply`가 있으면 apply를 중단합니다.
- `--no-hooks`: 반영 전·후 명령을 실행하지 않고 진행합니다. 필요한 명령은 직접 실행하세요.

서버 스크립트는 다음 안전장치를 갖고 있습니다.
- **패키지 검증**: `check`·`backup`·`apply` 전에 `checksums.sha256`으로 업로드 중 깨지거나 바뀐 파일이 없는지 확인합니다(`sha256sum` 또는 `shasum` 사용).
- **동시 실행 잠금**: 같은 대상 경로에 다른 사람이 `backup`·`apply`·`rollback` 중이면 멈춥니다. 비정상 종료로 남은 잠금은 자동으로 정리합니다.
- **배포 이력**: `backup`·`apply`·`rollback` 결과를 서버의 `~/.shipkit/history.log`에 남깁니다(실행한 계정, 대상, 패키지, 결과).

배포 파일은 `files/` 아래에 따로 들어 있습니다. 그래서 WinSCP 등으로 직접 옮길 때도 `files/` 안의 내용만 `server_path`로 복사하면 되고, 스크립트나 보고서가 웹 루트에 섞이지 않습니다.

대상 경로는 설정의 `server_path`가 기본값이고, 두 번째 인자로 바꿀 수 있습니다(`./shipkit.sh apply /app/other/ROOT`).

- `apply`는 서버와 내용이 같은 파일은 건드리지 않고, 바뀐 파일만 수정 시각을 '지금'으로 두어 WAS가 변경을 감지하게 합니다. WAS 재시작은 하지 않습니다.
- **sync 방식의 옛 파일 정리**: `prune.txt`에 적힌 서버 폴더(예: `assets/`) 안에서 이번 빌드에 없는 파일을 지웁니다. 정리 대상은 `backup` 때 확정되어 함께 백업되므로 `rollback`으로 되살릴 수 있습니다. 정리 폴더 밖(예: `uploads/`)은 건드리지 않습니다.

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
- sync 방식은 매번 빌드 결과 전체를 패키지에 담습니다(서버에는 바뀐 파일만 반영).
- 의존성 설치, DB 마이그레이션, WAS 재시작은 `hooks`에 직접 적어야 실행됩니다. 적지 않으면 알림만 띄웁니다.
- 서버 스크립트 메시지는 한국어만 지원합니다.
