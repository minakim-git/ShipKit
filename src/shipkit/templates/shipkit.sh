#!/usr/bin/env bash
# ShipKit @@APP_VERSION@@ - Safe Deployment & Rollback Tool
#   패키지: @@PACKAGE_NAME@@
#   생성  : @@CREATED_AT@@
#
# 사용법 (패키지 폴더에서 실행):
#   ./shipkit.sh check    [대상경로]   반영될 파일과 서버 현재 상태 비교 (서버 변경 없음)
#   ./shipkit.sh backup   [대상경로]   반영/삭제될 서버 파일을 이 폴더의 backup/ 에 보관
#   ./shipkit.sh apply    [대상경로]   files/ 반영 + delete.txt 삭제 (backup 선행 필수)
#   ./shipkit.sh rollback [대상경로]   backup/ 기준으로 apply 이전 상태로 복구
# 실행 권한이 없으면 'chmod +x shipkit.sh' 후 실행하거나 'bash shipkit.sh check' 처럼 실행하세요.
# 대상경로를 생략하면 TARGET_DEFAULT 를 사용합니다.

if [ -z "${BASH_VERSION:-}" ]; then
    echo "[오류] bash 로 실행하세요: ./shipkit.sh <명령> 또는 bash shipkit.sh <명령>" >&2
    exit 2
fi

set -u

TARGET_DEFAULT='@@SERVER_PATH@@'
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FILES_DIR="$SCRIPT_DIR/files"
MANIFEST="$SCRIPT_DIR/manifest.txt"
DELETES="$SCRIPT_DIR/delete.txt"
BACKUP_DIR="$SCRIPT_DIR/backup"
BACKUP_FILES="$BACKUP_DIR/files"
STATE_DIR="$BACKUP_DIR/.state"

die()  { echo "[오류] $*" >&2; exit 1; }
info() { echo "[정보] $*"; }
warn() { echo "[경고] $*" >&2; }

usage() {
    sed -n '/^# 사용법/,/^# 대상경로/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit 2
}

# 목록 파일을 한 줄씩 출력 (CR 제거, 빈 줄 무시)
read_list() {
    [ -f "$1" ] || return 0
    local line
    while IFS= read -r line || [ -n "$line" ]; do
        line="${line%$'\r'}"
        [ -n "$line" ] && printf '%s\n' "$line"
    done < "$1"
}

# 대상 경로 밖을 가리키는 항목 차단
check_rel() {
    case "$1" in
        /*|..|../*|*/..|*/../*) die "허용되지 않는 경로가 목록에 있습니다: $1" ;;
    esac
}

require_backup() {
    [ -f "$STATE_DIR/backup.done" ] || die "백업이 없습니다. 먼저 'bash shipkit.sh backup' 을 실행하세요."
    local saved
    saved="$(cat "$STATE_DIR/target")"
    [ "$saved" = "$TARGET" ] || die "백업한 대상($saved)과 지금 대상($TARGET)이 다릅니다."
}

cmd_check() {
    local rel new=0 changed=0 same=0 del=0 gone=0
    echo "== 반영 대상 (manifest.txt) =="
    while IFS= read -r rel; do
        check_rel "$rel"
        if [ ! -e "$TARGET/$rel" ]; then
            echo "  [신규] $rel"; new=$((new + 1))
        elif cmp -s "$FILES_DIR/$rel" "$TARGET/$rel"; then
            echo "  [동일] $rel"; same=$((same + 1))
        else
            echo "  [변경] $rel"; changed=$((changed + 1))
        fi
    done < <(read_list "$MANIFEST")
    echo "== 삭제 대상 (delete.txt) =="
    while IFS= read -r rel; do
        check_rel "$rel"
        if [ -e "$TARGET/$rel" ]; then
            echo "  [삭제예정] $rel"; del=$((del + 1))
        else
            echo "  [이미없음] $rel"; gone=$((gone + 1))
        fi
    done < <(read_list "$DELETES")
    info "신규 $new / 변경 $changed / 동일 $same / 삭제예정 $del / 이미없음 $gone"
}

cmd_backup() {
    [ -f "$STATE_DIR/backup.done" ] && die "이미 백업되어 있습니다($BACKUP_DIR). 원본 보호를 위해 덮어쓰지 않습니다. 다시 하려면 backup 폴더를 직접 지우세요."
    mkdir -p "$BACKUP_FILES" "$STATE_DIR" || die "백업 폴더를 만들 수 없습니다: $BACKUP_DIR"
    : > "$STATE_DIR/existed.txt"
    : > "$STATE_DIR/absent.txt"
    local rel count=0 absent=0
    while IFS= read -r rel; do
        check_rel "$rel"
        if [ -e "$TARGET/$rel" ]; then
            [ -f "$TARGET/$rel" ] || die "파일이 아닌 항목은 처리하지 않습니다: $TARGET/$rel"
            mkdir -p "$(dirname "$BACKUP_FILES/$rel")" || die "폴더 생성 실패: $BACKUP_FILES/$rel"
            cp -p "$TARGET/$rel" "$BACKUP_FILES/$rel" || die "백업 실패: $TARGET/$rel"
            printf '%s\n' "$rel" >> "$STATE_DIR/existed.txt"
            count=$((count + 1))
        else
            printf '%s\n' "$rel" >> "$STATE_DIR/absent.txt"
            absent=$((absent + 1))
        fi
    done < <({ read_list "$MANIFEST"; read_list "$DELETES"; } | sort -u)
    printf '%s\n' "$TARGET" > "$STATE_DIR/target"
    date '+%Y-%m-%d %H:%M:%S' > "$STATE_DIR/backup.done"
    info "백업 완료: 기존 파일 ${count}개 보관, 서버에 없던 파일 ${absent}개 기록"
    info "백업 위치: $BACKUP_DIR"
}

cmd_apply() {
    require_backup
    local rel ok=0 fail=0 removed=0
    while IFS= read -r rel; do
        check_rel "$rel"
        if [ ! -f "$FILES_DIR/$rel" ]; then
            warn "패키지에 파일이 없습니다: files/$rel"; fail=$((fail + 1)); continue
        fi
        # 수정 시각을 '지금'으로 두어 WAS 가 변경을 감지하도록 -p 를 쓰지 않는다.
        if mkdir -p "$(dirname "$TARGET/$rel")" && cp "$FILES_DIR/$rel" "$TARGET/$rel"; then
            ok=$((ok + 1))
        else
            warn "반영 실패: $rel"; fail=$((fail + 1))
        fi
    done < <(read_list "$MANIFEST")
    while IFS= read -r rel; do
        check_rel "$rel"
        if [ -f "$TARGET/$rel" ]; then
            if rm -f -- "$TARGET/$rel"; then removed=$((removed + 1)); else warn "삭제 실패: $rel"; fail=$((fail + 1)); fi
        elif [ -e "$TARGET/$rel" ]; then
            warn "파일이 아니라서 삭제하지 않았습니다: $rel"
        fi
    done < <(read_list "$DELETES")
    date '+%Y-%m-%d %H:%M:%S' > "$STATE_DIR/apply.done"
    info "반영 ${ok}개, 삭제 ${removed}개, 실패 ${fail}개"
    [ "$fail" -eq 0 ] || { warn "실패 항목이 있습니다. 확인 후 필요하면 'bash shipkit.sh rollback' 으로 복구하세요."; exit 1; }
}

cmd_rollback() {
    require_backup
    local rel restored=0 removed=0 fail=0
    while IFS= read -r rel; do
        check_rel "$rel"
        if mkdir -p "$(dirname "$TARGET/$rel")" && cp "$BACKUP_FILES/$rel" "$TARGET/$rel"; then
            restored=$((restored + 1))
        else
            warn "복구 실패: $rel"; fail=$((fail + 1))
        fi
    done < <(read_list "$STATE_DIR/existed.txt")
    while IFS= read -r rel; do
        check_rel "$rel"
        if [ -f "$TARGET/$rel" ]; then
            if rm -f -- "$TARGET/$rel"; then removed=$((removed + 1)); else warn "삭제 실패: $rel"; fail=$((fail + 1)); fi
        fi
    done < <(read_list "$STATE_DIR/absent.txt")
    rm -f "$STATE_DIR/apply.done"
    info "복구 ${restored}개, 신규 파일 제거 ${removed}개, 실패 ${fail}개"
    [ "$fail" -eq 0 ] || exit 1
}

CMD="${1:-}"
[ -n "$CMD" ] || usage
TARGET="${2:-$TARGET_DEFAULT}"
[ "$TARGET" = "/" ] || TARGET="${TARGET%/}"
[ -d "$TARGET" ] || die "대상 경로가 없습니다: $TARGET"
TARGET="$(cd "$TARGET" && pwd)"
[ -f "$MANIFEST" ] || die "manifest.txt 가 없습니다. 패키지 폴더 전체를 서버에 올렸는지 확인하세요."

info "대상 경로: $TARGET"
case "$CMD" in
    check)    cmd_check ;;
    backup)   cmd_backup ;;
    apply)    cmd_apply ;;
    rollback) cmd_rollback ;;
    *)        usage ;;
esac
