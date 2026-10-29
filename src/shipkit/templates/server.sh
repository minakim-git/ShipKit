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
#   ./@@SCRIPT_NAME@@ history               이 서버의 배포 이력 (최근 20건)
# 옵션: -y (반영 전·후 명령을 묻지 않고 실행), --no-hooks (반영 전·후 명령을 실행하지 않음)
# 실행 권한이 없으면 'chmod +x @@SCRIPT_NAME@@' 후 실행하거나 'bash @@SCRIPT_NAME@@ check' 처럼 실행하세요.
# 대상경로를 생략하면 TARGET_DEFAULT 를 사용합니다.

if [ -z "${BASH_VERSION:-}" ]; then
    echo "[오류] bash 로 실행하세요: ./@@SCRIPT_NAME@@ <명령> 또는 bash @@SCRIPT_NAME@@ <명령>" >&2
    exit 2
fi

set -u

SCRIPT_NAME='@@SCRIPT_NAME@@'
TARGET_DEFAULT='@@SERVER_PATH@@'
NEW_OWNER='@@OWNER@@'          # 새로 만든 파일·폴더 소유자 ("user:group", "inherit" = 상위 폴더와 같게)
NEW_FILE_MODE='@@FILE_MODE@@'  # 새로 만든 파일 권한
NEW_DIR_MODE='@@DIR_MODE@@'    # 새로 만든 폴더 권한
HISTORY_FILE='@@HISTORY_FILE@@'
LOCK_PREFIX='@@CLI_NAME@@'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PACKAGE_NAME="$(basename "$SCRIPT_DIR")"
FILES_DIR="$SCRIPT_DIR/files"
MANIFEST="$SCRIPT_DIR/manifest.txt"
DELETES="$SCRIPT_DIR/delete.txt"
PRUNE="$SCRIPT_DIR/prune.txt"
HOOKS_DIR="$SCRIPT_DIR/hooks"
CHECKSUMS="$SCRIPT_DIR/checksums.sha256"
BACKUP_DIR="$SCRIPT_DIR/backup"
BACKUP_FILES="$BACKUP_DIR/files"
STATE_DIR="$BACKUP_DIR/.state"
case "$HISTORY_FILE" in "~/"*) HISTORY_FILE="$HOME/${HISTORY_FILE:2}" ;; esac

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

# ------------------------------------------------------------------ 패키지 검증
verify_package() {
    if [ ! -f "$CHECKSUMS" ]; then
        warn "checksums.sha256 이 없어 패키지 검증을 건너뜁니다."
        return 0
    fi
    local tool out
    if command -v sha256sum >/dev/null 2>&1; then
        tool="sha256sum"
    elif command -v shasum >/dev/null 2>&1; then
        tool="shasum -a 256"
    else
        warn "sha256sum/shasum 이 없어 패키지 검증을 건너뜁니다."
        return 0
    fi
    # 성공 여부는 종료 코드로 판정한다. 출력 문구는 계정 언어 설정에 따라 번역되므로(OK/성공) 실패 줄을 보여주는 데만 쓴다.
    if out="$(cd "$SCRIPT_DIR" && LC_ALL=C $tool -c checksums.sha256 2>&1)"; then
        return 0
    fi
    echo "$out" | grep -v ': OK$' | sed 's/^/  /' >&2
    die "패키지 파일이 손상되었거나 바뀌었습니다 (checksums.sha256 불일치). 패키지를 다시 올려주세요."
}

# ------------------------------------------------------------------ 동시 실행 잠금
LOCK_DIR=""
release_lock() { [ -n "$LOCK_DIR" ] && rm -rf "$LOCK_DIR"; LOCK_DIR=""; }

acquire_lock() {
    local key dir pid
    key="$(printf '%s' "$TARGET" | cksum | cut -d' ' -f1)"
    dir="${TMPDIR:-/tmp}/${LOCK_PREFIX}-${key}.lock"
    if ! mkdir "$dir" 2>/dev/null; then
        pid="$(cat "$dir/pid" 2>/dev/null || true)"
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            die "같은 대상에 다른 배포가 진행 중입니다: $(cat "$dir/info" 2>/dev/null) (잠금: $dir)"
        fi
        warn "이전 실행이 남긴 잠금을 정리합니다: $dir"
        rm -rf "$dir"
        mkdir "$dir" 2>/dev/null || die "잠금을 만들 수 없습니다: $dir"
    fi
    LOCK_DIR="$dir"
    trap release_lock EXIT
    echo "$$" > "$LOCK_DIR/pid"
    echo "$(id -un 2>/dev/null || echo '?') $CMD $PACKAGE_NAME $(date '+%Y-%m-%d %H:%M:%S')" > "$LOCK_DIR/info"
}

# ------------------------------------------------------------------ 배포 이력
log_history() {
    mkdir -p "$(dirname "$HISTORY_FILE")" 2>/dev/null
    printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$(id -un 2>/dev/null || echo '?')" \
        "$CMD" "$TARGET" "$PACKAGE_NAME" "$*" >> "$HISTORY_FILE" 2>/dev/null \
        || warn "배포 이력을 기록하지 못했습니다: $HISTORY_FILE"
}

cmd_history() {
    if [ ! -f "$HISTORY_FILE" ]; then
        info "배포 이력이 없습니다: $HISTORY_FILE"
        return 0
    fi
    info "배포 이력 ($HISTORY_FILE, 최근 20건)"
    local t u c tg pk rs
    tail -n 20 "$HISTORY_FILE" | while IFS=$'\t' read -r t u c tg pk rs; do
        printf '  %s  %-10s %-9s %s  [%s]  %s\n' "$t" "$u" "$c" "$tg" "$pk" "$rs"
    done
}

# ------------------------------------------------------------------ 새 파일·폴더 소유자/권한
set_attrs() {
    local kind="$1" path="$2" mode
    if [ "$kind" = "dir" ]; then mode="$NEW_DIR_MODE"; else mode="$NEW_FILE_MODE"; fi
    if [ -n "$mode" ]; then
        chmod "$mode" "$path" 2>/dev/null || warn "권한 변경 실패($mode): $path"
    fi
    if [ "$NEW_OWNER" = "inherit" ]; then
        chown --reference="$(dirname "$path")" "$path" 2>/dev/null || warn "소유자 변경 실패(상위 폴더 따름): $path"
    elif [ -n "$NEW_OWNER" ]; then
        chown "$NEW_OWNER" "$path" 2>/dev/null || warn "소유자 변경 실패($NEW_OWNER): $path"
    fi
}

# 파일의 상위 폴더를 만들고, 새로 만든 폴더에만 소유자/권한을 적용한다.
ensure_parent() {
    local parent d i
    local -a missing=()
    parent="$(dirname "$1")"
    d="$parent"
    while [ ! -d "$d" ]; do missing+=("$d"); d="$(dirname "$d")"; done
    mkdir -p "$parent" || return 1
    for (( i=${#missing[@]}-1; i>=0; i-- )); do set_attrs dir "${missing[$i]}"; done
    return 0
}

# src 를 서버의 rel 위치로 복사. 새로 생긴 파일에만 소유자/권한을 적용한다.
put_file() {
    local src="$1" rel="$2" existed=0
    [ -e "$TARGET/$rel" ] && existed=1
    # 수정 시각을 '지금'으로 두어 WAS 가 변경을 감지하도록 -p 를 쓰지 않는다.
    ensure_parent "$TARGET/$rel" && cp "$src" "$TARGET/$rel" || return 1
    [ "$existed" -eq 0 ] && set_attrs file "$TARGET/$rel"
    return 0
}

# ------------------------------------------------------------------ 반영 전·후 명령 (hooks)
ASSUME_YES=0
RUN_HOOKS=1
hook_file() { printf '%s/%s.sh' "$HOOKS_DIR" "$1"; }
has_hook() { [ -f "$(hook_file "$1")" ]; }
show_hook() { grep -v -e '^#' -e '^set -e$' -e '^$' "$(hook_file "$1")" | sed 's/^/    /'; }

# 반환: 0 = 실행 성공(또는 명령 없음), 1 = 실행 실패, 2 = 실행하지 않음
run_hook() {
    local stage="$1" file rc ans
    has_hook "$stage" || return 0
    file="$(hook_file "$stage")"
    echo "== $stage 명령 =="
    show_hook "$stage"
    if [ "$RUN_HOOKS" -eq 0 ]; then
        warn "--no-hooks: $stage 명령을 실행하지 않았습니다. 필요하면 직접 실행하세요."
        return 2
    fi
    if [ "$ASSUME_YES" -ne 1 ]; then
        if [ -t 0 ]; then
            read -r -p "위 명령을 실행할까요? [y/N] " ans
            case "$ans" in
                y|Y|yes|YES) ;;
                *) warn "$stage 명령을 실행하지 않았습니다."; return 2 ;;
            esac
        else
            warn "대화형 터미널이 아니어서 $stage 명령을 실행하지 않았습니다 (-y 를 붙이면 실행)."
            return 2
        fi
    fi
    (cd "$TARGET" && DEPLOY_TARGET="$TARGET" DEPLOY_PACKAGE_DIR="$SCRIPT_DIR" DEPLOY_PACKAGE="$PACKAGE_NAME" \
        bash "$file")
    rc=$?
    if [ "$rc" -eq 0 ]; then
        info "$stage 명령 완료"
        return 0
    fi
    warn "$stage 명령 실패 (종료 코드 $rc)"
    return 1
}

# ------------------------------------------------------------------ prune
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

# ------------------------------------------------------------------ 명령
cmd_check() {
    verify_package
    local rel new=0 changed=0 same=0 del=0 gone=0 pruned=0 total show_same stage
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
    for stage in pre_apply post_apply post_rollback; do
        if has_hook "$stage"; then
            echo "== $stage 명령 (실행 전에 확인을 받습니다, -y 로 생략) =="
            show_hook "$stage"
        fi
    done
    if [ -n "$NEW_OWNER$NEW_FILE_MODE$NEW_DIR_MODE" ]; then
        info "새 파일·폴더 설정: 소유자 ${NEW_OWNER:-그대로} / 파일 권한 ${NEW_FILE_MODE:-그대로} / 폴더 권한 ${NEW_DIR_MODE:-그대로}"
    fi
    info "신규 $new / 변경 $changed / 동일 $same / 삭제예정 $del / 이미없음 $gone / 정리예정 $pruned"
}

cmd_backup() {
    [ -f "$STATE_DIR/backup.done" ] && die "이미 백업되어 있습니다($BACKUP_DIR). 원본 보호를 위해 덮어쓰지 않습니다. 다시 하려면 backup 폴더를 직접 지우세요."
    verify_package
    acquire_lock
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
    log_history "보관 ${count}개"
}

# 서버에서 지우기 (파일만). 반환: 0 삭제, 1 실패, 2 해당 없음
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
    verify_package
    acquire_lock
    local rel ok=0 same=0 fail=0 removed=0 pruned=0 rc summary
    run_hook pre_apply; rc=$?
    if [ "$rc" -eq 1 ]; then
        log_history "중단: pre_apply 실패"
        die "pre_apply 명령이 실패해서 apply 를 중단했습니다. 서버 파일은 바뀌지 않았습니다."
    elif [ "$rc" -eq 2 ] && [ "$RUN_HOOKS" -eq 1 ]; then
        die "pre_apply 명령을 실행하지 않아 apply 를 중단했습니다. 서버 파일은 바뀌지 않았습니다. (명령 없이 진행하려면 --no-hooks)"
    fi
    while IFS= read -r rel; do
        check_rel "$rel"
        if [ ! -f "$FILES_DIR/$rel" ]; then
            warn "패키지에 파일이 없습니다: files/$rel"; fail=$((fail + 1)); continue
        fi
        # 서버와 같은 파일은 건드리지 않는다 (수정 시각이 바뀌어 불필요한 재로딩·캐시 무효화가 생기지 않게).
        if [ -f "$TARGET/$rel" ] && cmp -s "$FILES_DIR/$rel" "$TARGET/$rel"; then
            same=$((same + 1)); continue
        fi
        if put_file "$FILES_DIR/$rel" "$rel"; then
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
    summary="반영 ${ok}개, 동일(건너뜀) ${same}개, 삭제 ${removed}개, 정리 ${pruned}개, 실패 ${fail}개"
    info "$summary"
    if [ "$fail" -ne 0 ]; then
        log_history "$summary"
        has_hook post_apply && warn "실패 항목이 있어 post_apply 명령은 실행하지 않았습니다."
        warn "실패 항목이 있습니다. 확인 후 필요하면 './$SCRIPT_NAME rollback' 으로 복구하세요."
        exit 1
    fi
    run_hook post_apply; rc=$?
    if [ "$rc" -eq 1 ]; then
        log_history "$summary, post_apply 실패"
        warn "파일은 반영됐지만 post_apply 명령이 실패했습니다. 확인 후 필요하면 './$SCRIPT_NAME rollback' 으로 복구하세요."
        exit 1
    fi
    log_history "$summary"
}

cmd_rollback() {
    require_backup
    acquire_lock
    local rel restored=0 removed=0 fail=0 rc summary
    while IFS= read -r rel; do
        check_rel "$rel"
        if put_file "$BACKUP_FILES/$rel" "$rel"; then
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
    summary="복구 ${restored}개, 신규 파일 제거 ${removed}개, 실패 ${fail}개"
    info "$summary"
    if [ "$fail" -ne 0 ]; then
        log_history "$summary"
        exit 1
    fi
    run_hook post_rollback; rc=$?
    if [ "$rc" -eq 1 ]; then
        log_history "$summary, post_rollback 실패"
        exit 1
    fi
    log_history "$summary"
}

# ------------------------------------------------------------------ 시작
CMD=""
TARGET_ARG=""
for arg in "$@"; do
    case "$arg" in
        -y|--yes)   ASSUME_YES=1 ;;
        --no-hooks) RUN_HOOKS=0 ;;
        -h|--help)  usage ;;
        -*)         die "알 수 없는 옵션입니다: $arg" ;;
        *)
            if [ -z "$CMD" ]; then CMD="$arg"
            elif [ -z "$TARGET_ARG" ]; then TARGET_ARG="$arg"
            else die "인자가 너무 많습니다: $arg"
            fi ;;
    esac
done
[ -n "$CMD" ] || usage
if [ "$CMD" = "history" ]; then
    cmd_history
    exit 0
fi

TARGET="${TARGET_ARG:-$TARGET_DEFAULT}"
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
