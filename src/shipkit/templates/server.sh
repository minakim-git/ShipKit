#!/usr/bin/env bash
# @@APP_NAME@@ @@APP_VERSION@@ - @@TAGLINE@@
#   패키지: @@PACKAGE_NAME@@
#   생성  : @@CREATED_AT@@
#
# 사용법 (패키지 폴더에서 실행):
#   ./@@SCRIPT_NAME@@ check    [대상경로]   반영될 파일과 서버 현재 상태 비교 (서버 변경 없음)
#   ./@@SCRIPT_NAME@@ backup   [대상경로]   바뀌거나 지워질 서버 파일을 이 폴더의 backup/ 에 보관
#   ./@@SCRIPT_NAME@@ apply    [대상경로]   files/ 반영 + 삭제·정리 대상 삭제 (backup 선행 필수)
#   ./@@SCRIPT_NAME@@ rollback [대상경로]   backup/ 기준으로 apply 이전 상태로 복구
# 실행 권한이 없으면 'chmod +x @@SCRIPT_NAME@@' 후 실행하거나 'bash @@SCRIPT_NAME@@ check' 처럼 실행하세요.
# 대상경로를 생략하면 TARGET_DEFAULT 를 사용합니다.

if [ -z "${BASH_VERSION:-}" ]; then
    echo "[오류] bash 로 실행하세요: ./@@SCRIPT_NAME@@ <명령> 또는 bash @@SCRIPT_NAME@@ <명령>" >&2
    exit 2
fi

set -u

SCRIPT_NAME='@@SCRIPT_NAME@@'
TARGET_DEFAULT='@@SERVER_PATH@@'
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FILES_DIR="$SCRIPT_DIR/files"
MANIFEST="$SCRIPT_DIR/manifest.txt"
DELETES="$SCRIPT_DIR/delete.txt"
PRUNE="$SCRIPT_DIR/prune.txt"
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

has_prune() { [ -n "$(read_list "$PRUNE")" ]; }

# prune.txt 의 서버 폴더 안에서 이번 빌드(manifest)에 없는 파일 = 정리할 옛 빌드 파일
prune_candidates() {
    local dir base f rel self_rel=""
    case "$SCRIPT_DIR/" in "$TARGET"/*) self_rel="${SCRIPT_DIR#"$TARGET"/}/" ;; esac
    {
        while IFS= read -r dir; do
            if [ "$dir" = "." ]; then
                base="$TARGET"
            else
                check_rel "$dir"
                base="$TARGET/$dir"
            fi
            [ -d "$base" ] || continue
            while IFS= read -r f; do
                rel="${f#"$TARGET"/}"
                if [ -n "$self_rel" ]; then
                    case "$rel" in "$self_rel"*) continue ;; esac  # 패키지 폴더 자신은 건드리지 않는다
                fi
                printf '%s\n' "$rel"
            done < <(find "$base" -type f -print 2>/dev/null)
        done < <(read_list "$PRUNE")
    } | LC_ALL=C sort -u | grep -Fxv -f <(read_list "$MANIFEST"; echo "//none//") || true
}

require_backup() {
    [ -f "$STATE_DIR/backup.done" ] || die "백업이 없습니다. 먼저 './$SCRIPT_NAME backup' 을 실행하세요."
    local saved
    saved="$(cat "$STATE_DIR/target")"
    [ "$saved" = "$TARGET" ] || die "백업한 대상($saved)과 지금 대상($TARGET)이 다릅니다."
}

cmd_check() {
    local rel new=0 changed=0 same=0 del=0 gone=0 pruned=0 total show_same
    total="$(read_list "$MANIFEST" | wc -l)"
    # 파일이 많으면(빌드 결과 동기화 등) 같은 파일은 개수만 보여준다.
    show_same=1; [ "$total" -gt 100 ] && show_same=0
    echo "== 반영 대상 (manifest.txt) =="
    while IFS= read -r rel; do
        check_rel "$rel"
        if [ ! -e "$TARGET/$rel" ]; then
            echo "  [신규] $rel"; new=$((new + 1))
        elif cmp -s "$FILES_DIR/$rel" "$TARGET/$rel"; then
            [ "$show_same" -eq 1 ] && echo "  [동일] $rel"
            same=$((same + 1))
        else
            echo "  [변경] $rel"; changed=$((changed + 1))
        fi
    done < <(read_list "$MANIFEST")
    [ "$same" -gt 0 ] && echo "  (서버와 같은 파일 ${same}개는 apply 때 건드리지 않습니다)"
    echo "== 삭제 대상 (delete.txt) =="
    while IFS= read -r rel; do
        check_rel "$rel"
        if [ -e "$TARGET/$rel" ]; then
            echo "  [삭제예정] $rel"; del=$((del + 1))
        else
            echo "  [이미없음] $rel"; gone=$((gone + 1))
        fi
    done < <(read_list "$DELETES")
    if has_prune; then
        echo "== 정리 대상 (prune.txt 폴더 안에서 이번 빌드에 없는 옛 파일) =="
        while IFS= read -r rel; do
            echo "  [정리예정] $rel"; pruned=$((pruned + 1))
        done < <(prune_candidates)
    fi
    info "신규 $new / 변경 $changed / 동일 $same / 삭제예정 $del / 이미없음 $gone / 정리예정 $pruned"
}

cmd_backup() {
    [ -f "$STATE_DIR/backup.done" ] && die "이미 백업되어 있습니다($BACKUP_DIR). 원본 보호를 위해 덮어쓰지 않습니다. 다시 하려면 backup 폴더를 직접 지우세요."
    mkdir -p "$BACKUP_FILES" "$STATE_DIR" || die "백업 폴더를 만들 수 없습니다: $BACKUP_DIR"
    : > "$STATE_DIR/existed.txt"
    : > "$STATE_DIR/absent.txt"
    # 정리 대상은 백업 시점에 확정한다. apply 와 rollback 은 이 목록만 쓴다.
    if has_prune; then prune_candidates > "$STATE_DIR/pruned.txt"; else : > "$STATE_DIR/pruned.txt"; fi
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
    done < <({ read_list "$MANIFEST"; read_list "$DELETES"; read_list "$STATE_DIR/pruned.txt"; } | LC_ALL=C sort -u)
    printf '%s\n' "$TARGET" > "$STATE_DIR/target"
    date '+%Y-%m-%d %H:%M:%S' > "$STATE_DIR/backup.done"
    info "백업 완료: 기존 파일 ${count}개 보관, 서버에 없던 파일 ${absent}개 기록"
    info "백업 위치: $BACKUP_DIR"
}

# 서버에서 지우기 (파일만)
remove_file() {
    local rel="$1"
    check_rel "$rel"
    if [ -f "$TARGET/$rel" ]; then
        rm -f -- "$TARGET/$rel" && return 0
        warn "삭제 실패: $rel"; return 1
    elif [ -e "$TARGET/$rel" ]; then
        warn "파일이 아니라서 삭제하지 않았습니다: $rel"
    fi
    return 2
}

cmd_apply() {
    require_backup
    local rel ok=0 same=0 fail=0 removed=0 pruned=0 rc
    while IFS= read -r rel; do
        check_rel "$rel"
        if [ ! -f "$FILES_DIR/$rel" ]; then
            warn "패키지에 파일이 없습니다: files/$rel"; fail=$((fail + 1)); continue
        fi
        # 서버와 같은 파일은 건드리지 않는다 (수정 시각이 바뀌어 불필요한 재로딩·캐시 무효화가 생기지 않게).
        if [ -f "$TARGET/$rel" ] && cmp -s "$FILES_DIR/$rel" "$TARGET/$rel"; then
            same=$((same + 1)); continue
        fi
        # 수정 시각을 '지금'으로 두어 WAS 가 변경을 감지하도록 -p 를 쓰지 않는다.
        if mkdir -p "$(dirname "$TARGET/$rel")" && cp "$FILES_DIR/$rel" "$TARGET/$rel"; then
            ok=$((ok + 1))
        else
            warn "반영 실패: $rel"; fail=$((fail + 1))
        fi
    done < <(read_list "$MANIFEST")
    while IFS= read -r rel; do
        remove_file "$rel"; rc=$?
        [ "$rc" -eq 0 ] && removed=$((removed + 1)); [ "$rc" -eq 1 ] && fail=$((fail + 1))
    done < <(read_list "$DELETES")
    while IFS= read -r rel; do
        remove_file "$rel"; rc=$?
        [ "$rc" -eq 0 ] && pruned=$((pruned + 1)); [ "$rc" -eq 1 ] && fail=$((fail + 1))
    done < <(read_list "$STATE_DIR/pruned.txt")
    date '+%Y-%m-%d %H:%M:%S' > "$STATE_DIR/apply.done"
    info "반영 ${ok}개, 동일(건너뜀) ${same}개, 삭제 ${removed}개, 정리 ${pruned}개, 실패 ${fail}개"
    [ "$fail" -eq 0 ] || { warn "실패 항목이 있습니다. 확인 후 필요하면 './$SCRIPT_NAME rollback' 으로 복구하세요."; exit 1; }
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
