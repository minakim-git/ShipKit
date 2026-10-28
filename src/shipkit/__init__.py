"""ShipKit: 변경 파일을 서버 경로에 맞게 패키지로 만들고, 백업·반영·원복까지 처리하는 배포 도구.

툴 이름과 파일 이름은 모두 여기서 관리한다. 이름을 바꿀 때는 이 파일만 고치면 된다.
"""

__app_name__ = "ShipKit"
__tagline__ = "Safe Deployment & Rollback Tool"
__version__ = "0.2.0"

CLI_NAME = "shipkit"                   # 명령어, 키링 서비스, 사용자 설정 폴더 이름
SCRIPT_NAME = f"{CLI_NAME}.sh"         # 패키지에 들어가는 서버 스크립트
CONFIG_NAME = f"{CLI_NAME}.yaml"       # 설정 파일
CONFIG_ENV = f"{CLI_NAME.upper()}_CONFIG"  # 설정 파일 경로 환경변수
