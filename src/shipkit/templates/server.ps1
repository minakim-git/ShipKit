# @@APP_NAME@@ @@APP_VERSION@@ - @@TAGLINE@@ (Windows 서버용)
#   패키지: @@PACKAGE_NAME@@
#   생성  : @@CREATED_AT@@
#
# 사용법 (패키지 폴더에서 실행):
#   powershell -ExecutionPolicy Bypass -File .\@@SCRIPT_NAME@@ check    [대상경로]   반영될 파일과 서버 현재 상태 비교 (서버 변경 없음)
#   powershell -ExecutionPolicy Bypass -File .\@@SCRIPT_NAME@@ backup   [대상경로]   바뀌거나 지워질 서버 파일을 이 폴더의 backup\ 에 보관
#   powershell -ExecutionPolicy Bypass -File .\@@SCRIPT_NAME@@ apply    [대상경로]   files\ 반영 + 삭제·정리 대상 삭제 (backup 선행 필수)
#   powershell -ExecutionPolicy Bypass -File .\@@SCRIPT_NAME@@ rollback [대상경로]   backup\ 기준으로 apply 이전 상태로 복구
#   powershell -ExecutionPolicy Bypass -File .\@@SCRIPT_NAME@@ history               이 서버의 배포 이력 (최근 20건)
# 옵션: -Yes (반영 전·후 명령을 묻지 않고 실행), -NoHooks (반영 전·후 명령을 실행하지 않음)
# 대상경로를 생략하면 TargetDefault 를 사용합니다.

param(
    [Parameter(Position = 0)][string]$Command = "",
    [Parameter(Position = 1)][string]$Target = "",
    [Alias("y")][switch]$Yes,
    [switch]$NoHooks
)

Set-StrictMode -Version 2
$ErrorActionPreference = "Stop"

$ScriptName = '@@SCRIPT_NAME@@'
$TargetDefault = '@@SERVER_PATH@@'
$HistoryFile = '@@HISTORY_FILE@@'
$LockPrefix = '@@CLI_NAME@@'

$ScriptDir = $PSScriptRoot
$PackageName = Split-Path -Leaf $ScriptDir
$FilesDir = Join-Path $ScriptDir "files"
$Manifest = Join-Path $ScriptDir "manifest.txt"
$Deletes = Join-Path $ScriptDir "delete.txt"
$Prune = Join-Path $ScriptDir "prune.txt"
$HooksDir = Join-Path $ScriptDir "hooks"
$Checksums = Join-Path $ScriptDir "checksums.sha256"
$BackupDir = Join-Path $ScriptDir "backup"
$BackupFiles = Join-Path $BackupDir "files"
$StateDir = Join-Path $BackupDir ".state"
if ($HistoryFile.StartsWith("~/")) { $HistoryFile = Join-Path $HOME ($HistoryFile.Substring(2) -replace "/", "\") }
$script:LockDir = $null

function Die([string]$Message) { Write-Host "[오류] $Message"; Release-Lock; exit 1 }
function Info([string]$Message) { Write-Host "[정보] $Message" }
function Warn([string]$Message) { Write-Host "[경고] $Message" }

function Show-Usage {
    $lines = Get-Content -LiteralPath $PSCommandPath -Encoding UTF8
    $on = $false
    foreach ($line in $lines) {
        if ($line -like "# 사용법*") { $on = $true }
        if ($on) { Write-Host ($line -replace "^# ?", "") }
        if ($line -like "# 대상경로*") { break }
    }
    exit 2
}

# 목록 파일을 줄 배열로 (빈 줄 무시)
function Read-List([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return @() }
    return @(Get-Content -LiteralPath $Path -Encoding UTF8 | ForEach-Object { $_.TrimEnd("`r") } | Where-Object { $_ -ne "" })
}

# 대상 경로 밖을 가리키는 항목 차단
function Check-Rel([string]$Rel) {
    if ($Rel -match '^([/\\]|[A-Za-z]:)' -or ($Rel -split '[/\\]') -contains "..") {
        Die "허용되지 않는 경로가 목록에 있습니다: $Rel"
    }
}

function Server-Path([string]$Rel) { return Join-Path $script:Target ($Rel -replace "/", "\") }
function Package-Path([string]$Base, [string]$Rel) { return Join-Path $Base ($Rel -replace "/", "\") }

function Same-File([string]$A, [string]$B) {
    if (-not (Test-Path -LiteralPath $A -PathType Leaf) -or -not (Test-Path -LiteralPath $B -PathType Leaf)) { return $false }
    if ((Get-Item -LiteralPath $A).Length -ne (Get-Item -LiteralPath $B).Length) { return $false }
    return (Get-FileHash -LiteralPath $A -Algorithm SHA256).Hash -eq (Get-FileHash -LiteralPath $B -Algorithm SHA256).Hash
}

# ------------------------------------------------------------------ 패키지 검증
function Verify-Package {
    if (-not (Test-Path -LiteralPath $Checksums)) { Warn "checksums.sha256 이 없어 패키지 검증을 건너뜁니다."; return }
    $bad = @()
    foreach ($line in (Read-List $Checksums)) {
        $parts = $line -split "  ", 2
        if ($parts.Count -ne 2) { continue }
        $file = Package-Path $ScriptDir $parts[1]
        if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { $bad += "  $($parts[1]): 없음"; continue }
        if ((Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash -ne $parts[0].ToUpper()) { $bad += "  $($parts[1]): 불일치" }
    }
    if ($bad.Count -gt 0) {
        $bad | ForEach-Object { Write-Host $_ }
        Die "패키지 파일이 손상되었거나 바뀌었습니다 (checksums.sha256 불일치). 패키지를 다시 올려주세요."
    }
}

# ------------------------------------------------------------------ 동시 실행 잠금
function Release-Lock {
    if ($script:LockDir -and (Test-Path -LiteralPath $script:LockDir)) { Remove-Item -LiteralPath $script:LockDir -Recurse -Force -ErrorAction SilentlyContinue }
    $script:LockDir = $null
}

function Acquire-Lock {
    $sha = [Security.Cryptography.SHA256]::Create()
    $bytes = $sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($script:Target.ToLowerInvariant()))
    $key = (($bytes[0..7] | ForEach-Object { $_.ToString("x2") }) -join "")
    $dir = Join-Path $env:TEMP "$LockPrefix-$key.lock"
    if (Test-Path -LiteralPath $dir) {
        $pidFile = Join-Path $dir "pid"
        $owner = if (Test-Path -LiteralPath $pidFile) { (Get-Content -LiteralPath $pidFile -Raw).Trim() } else { "" }
        if ($owner -match '^\d+$' -and (Get-Process -Id ([int]$owner) -ErrorAction SilentlyContinue)) {
            $info = Get-Content -LiteralPath (Join-Path $dir "info") -Raw -ErrorAction SilentlyContinue
            Die "같은 대상에 다른 배포가 진행 중입니다: $info (잠금: $dir)"
        }
        Warn "이전 실행이 남긴 잠금을 정리합니다: $dir"
        Remove-Item -LiteralPath $dir -Recurse -Force
    }
    New-Item -ItemType Directory -Path $dir -ErrorAction Stop | Out-Null
    $script:LockDir = $dir
    Set-Content -LiteralPath (Join-Path $dir "pid") -Value $PID -Encoding ASCII
    Set-Content -LiteralPath (Join-Path $dir "info") -Value "$env:USERNAME $Command $PackageName $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" -Encoding UTF8
}

# ------------------------------------------------------------------ 배포 이력
function Log-History([string]$Result) {
    try {
        $dir = Split-Path -Parent $HistoryFile
        if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
        $line = "{0}`t{1}`t{2}`t{3}`t{4}`t{5}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $env:USERNAME, $Command, $script:Target, $PackageName, $Result
        Add-Content -LiteralPath $HistoryFile -Value $line -Encoding UTF8
    } catch {
        Warn "배포 이력을 기록하지 못했습니다: $HistoryFile"
    }
}

function Cmd-History {
    if (-not (Test-Path -LiteralPath $HistoryFile)) { Info "배포 이력이 없습니다: $HistoryFile"; return }
    Info "배포 이력 ($HistoryFile, 최근 20건)"
    Get-Content -LiteralPath $HistoryFile -Encoding UTF8 -Tail 20 | ForEach-Object {
        $f = $_ -split "`t"
        if ($f.Count -ge 6) { Write-Host ("  {0}  {1,-10} {2,-9} {3}  [{4}]  {5}" -f $f[0], $f[1], $f[2], $f[3], $f[4], $f[5]) }
    }
}

# ------------------------------------------------------------------ 파일 복사
# 수정 시각을 '지금'으로 두어 WAS 가 변경을 감지하게 한다 (Copy-Item 은 원본 시각을 유지하므로 따로 맞춘다).
# apply 중($script:RecordDirs)에 새로 만든 폴더는 기록해 두었다가 rollback 때 비어 있으면 지운다
$script:RecordDirs = $false
$script:CreatedDirs = @()
function Put-File([string]$Source, [string]$Rel) {
    $dest = Server-Path $Rel
    $parent = Split-Path -Parent $dest
    if (-not (Test-Path -LiteralPath $parent)) {
        $missing = @(); $d = $parent
        while ($d -and -not (Test-Path -LiteralPath $d)) { $missing += $d; $d = Split-Path -Parent $d }
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
        if ($script:RecordDirs) { $script:CreatedDirs += $missing }
    }
    Copy-Item -LiteralPath $Source -Destination $dest -Force
    (Get-Item -LiteralPath $dest).LastWriteTime = Get-Date
}

# apply 가 만든 폴더 가운데 비어 있는 것을 깊은 곳부터 지운다 (원래 있던 폴더는 건드리지 않는다). 반환: 지운 개수
function Remove-CreatedDirs {
    $n = 0
    $prefix = $script:Target.TrimEnd("\") + "\"
    foreach ($d in (Read-List (Join-Path $StateDir "created_dirs.txt") | Sort-Object -Unique -Descending)) {
        if (-not $d.StartsWith($prefix)) { continue }
        if ((Test-Path -LiteralPath $d -PathType Container) -and -not (Get-ChildItem -LiteralPath $d -Force | Select-Object -First 1)) {
            Remove-Item -LiteralPath $d -Force -ErrorAction SilentlyContinue
            if (-not (Test-Path -LiteralPath $d)) { $n++ }
        }
    }
    return $n
}

# ------------------------------------------------------------------ 반영 전·후 명령 (hooks)
function Hook-File([string]$Stage) { return Join-Path $HooksDir "$Stage.ps1" }
function Has-Hook([string]$Stage) { return Test-Path -LiteralPath (Hook-File $Stage) }
function Show-Hook([string]$Stage) {
    # 사용자가 적은 명령만 보여준다 (주석과 자동으로 붙인 '#@' 줄 제외)
    Get-Content -LiteralPath (Hook-File $Stage) -Encoding UTF8 | Where-Object { $_ -ne "" -and -not $_.StartsWith("#") -and -not $_.EndsWith("#@") } | ForEach-Object { Write-Host "    $_" }
}

# 반환: 0 = 실행 성공(또는 명령 없음), 1 = 실행 실패, 2 = 실행하지 않음
function Run-Hook([string]$Stage) {
    if (-not (Has-Hook $Stage)) { return 0 }
    Write-Host "== $Stage 명령 =="
    Show-Hook $Stage
    if ($NoHooks) { Warn "-NoHooks: $Stage 명령을 실행하지 않았습니다. 필요하면 직접 실행하세요."; return 2 }
    if (-not $Yes) {
        if ([Environment]::UserInteractive -and -not [Console]::IsInputRedirected) {
            $answer = Read-Host "위 명령을 실행할까요? [y/N]"
            if ($answer -notmatch '^(y|yes)$') { Warn "$Stage 명령을 실행하지 않았습니다."; return 2 }
        } else {
            Warn "대화형 터미널이 아니어서 $Stage 명령을 실행하지 않았습니다 (-Yes 를 붙이면 실행)."
            return 2
        }
    }
    $env:DEPLOY_TARGET = $script:Target
    $env:DEPLOY_PACKAGE_DIR = $ScriptDir
    $env:DEPLOY_PACKAGE = $PackageName
    Push-Location -LiteralPath $script:Target
    try {
        & powershell -NoProfile -ExecutionPolicy Bypass -File (Hook-File $Stage) | Out-Host
        $rc = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    if ($rc -eq 0) { Info "$Stage 명령 완료"; return 0 }
    Warn "$Stage 명령 실패 (종료 코드 $rc)"
    return 1
}

# ------------------------------------------------------------------ prune
function Has-Prune { return @(Read-List $Prune).Count -gt 0 }

# prune.txt 의 서버 폴더 안에서 이번 빌드(manifest)에 없는 파일 = 정리할 옛 빌드 파일
function Prune-Candidates {
    $keep = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    foreach ($m in (Read-List $Manifest)) { [void]$keep.Add($m) }
    $self = $ScriptDir.TrimEnd("\") + "\"
    $found = New-Object 'System.Collections.Generic.SortedSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    $root = $script:Target.TrimEnd("\") + "\"
    foreach ($dir in (Read-List $Prune)) {
        if ($dir -eq ".") { $base = $script:Target } else { Check-Rel $dir; $base = Server-Path $dir }
        if (-not (Test-Path -LiteralPath $base -PathType Container)) { continue }
        foreach ($f in (Get-ChildItem -LiteralPath $base -Recurse -File -Force)) {
            if ($f.FullName.StartsWith($self, [StringComparison]::OrdinalIgnoreCase)) { continue }  # 패키지 폴더 자신은 제외
            $rel = $f.FullName.Substring($root.Length) -replace "\\", "/"
            if (-not $keep.Contains($rel)) { [void]$found.Add($rel) }
        }
    }
    return @($found)
}

function Require-Backup {
    if (-not (Test-Path -LiteralPath (Join-Path $StateDir "backup.done"))) { Die "백업이 없습니다. 먼저 '$ScriptName backup' 을 실행하세요." }
    $saved = (Get-Content -LiteralPath (Join-Path $StateDir "target") -Encoding UTF8 -Raw).Trim()
    if ($saved -ne $script:Target) { Die "백업한 대상($saved)과 지금 대상($($script:Target))이 다릅니다." }
}

function Write-List([string]$Path, [string[]]$Lines) {
    $Lines = @($Lines | Where-Object { $_ })
    $text = if ($Lines.Count -gt 0) { ($Lines -join "`n") + "`n" } else { "" }
    [IO.File]::WriteAllText($Path, $text, (New-Object Text.UTF8Encoding $false))
}

# ------------------------------------------------------------------ 명령
function Cmd-Check {
    Verify-Package
    $new = 0; $changed = 0; $same = 0; $del = 0; $gone = 0; $pruned = 0
    $list = @(Read-List $Manifest)
    $showSame = $list.Count -le 100
    Write-Host "== 반영 대상 (manifest.txt) =="
    foreach ($rel in $list) {
        Check-Rel $rel
        $dest = Server-Path $rel
        if (-not (Test-Path -LiteralPath $dest)) { Write-Host "  [신규] $rel"; $new++ }
        elseif (Same-File (Package-Path $FilesDir $rel) $dest) { if ($showSame) { Write-Host "  [동일] $rel" }; $same++ }
        else { Write-Host "  [변경] $rel"; $changed++ }
    }
    if ($same -gt 0) { Write-Host "  (서버와 같은 파일 ${same}개는 apply 때 건드리지 않습니다)" }
    Write-Host "== 삭제 대상 (delete.txt) =="
    foreach ($rel in (Read-List $Deletes)) {
        Check-Rel $rel
        if (Test-Path -LiteralPath (Server-Path $rel)) { Write-Host "  [삭제예정] $rel"; $del++ } else { Write-Host "  [이미없음] $rel"; $gone++ }
    }
    if (Has-Prune) {
        Write-Host "== 정리 대상 (prune.txt 폴더 안에서 이번 빌드에 없는 옛 파일) =="
        foreach ($rel in (Prune-Candidates)) { Write-Host "  [정리예정] $rel"; $pruned++ }
    }
    foreach ($stage in @("pre_apply", "post_apply", "post_rollback")) {
        if (Has-Hook $stage) { Write-Host "== $stage 명령 (실행 전에 확인을 받습니다, -Yes 로 생략) =="; Show-Hook $stage }
    }
    Info "신규 $new / 변경 $changed / 동일 $same / 삭제예정 $del / 이미없음 $gone / 정리예정 $pruned"
}

function Cmd-Backup {
    if (Test-Path -LiteralPath (Join-Path $StateDir "backup.done")) { Die "이미 백업되어 있습니다($BackupDir). 원본 보호를 위해 덮어쓰지 않습니다. 다시 하려면 backup 폴더를 직접 지우세요." }
    Verify-Package
    Acquire-Lock
    New-Item -ItemType Directory -Path $BackupFiles -Force | Out-Null
    New-Item -ItemType Directory -Path $StateDir -Force | Out-Null
    $prunedList = @(if (Has-Prune) { Prune-Candidates })
    Write-List (Join-Path $StateDir "pruned.txt") $prunedList
    $existed = @(); $absent = @()
    $all = New-Object 'System.Collections.Generic.SortedSet[string]' ([StringComparer]::Ordinal)
    foreach ($rel in (@(Read-List $Manifest) + @(Read-List $Deletes) + $prunedList)) { [void]$all.Add($rel) }
    foreach ($rel in $all) {
        Check-Rel $rel
        $src = Server-Path $rel
        if (Test-Path -LiteralPath $src) {
            if (-not (Test-Path -LiteralPath $src -PathType Leaf)) { Die "파일이 아닌 항목은 처리하지 않습니다: $src" }
            $dest = Package-Path $BackupFiles $rel
            New-Item -ItemType Directory -Path (Split-Path -Parent $dest) -Force | Out-Null
            Copy-Item -LiteralPath $src -Destination $dest -Force
            $existed += $rel
        } else {
            $absent += $rel
        }
    }
    Write-List (Join-Path $StateDir "existed.txt") $existed
    Write-List (Join-Path $StateDir "absent.txt") $absent
    Write-List (Join-Path $StateDir "target") @($script:Target)
    Write-List (Join-Path $StateDir "backup.done") @((Get-Date -Format "yyyy-MM-dd HH:mm:ss"))
    Info "백업 완료: 기존 파일 $($existed.Count)개 보관, 서버에 없던 파일 $($absent.Count)개 기록"
    Info "백업 위치: $BackupDir"
    Log-History "보관 $($existed.Count)개"
}

# 반환: 0 삭제, 1 실패, 2 해당 없음
function Remove-ServerFile([string]$Rel) {
    Check-Rel $Rel
    $path = Server-Path $Rel
    if (Test-Path -LiteralPath $path -PathType Leaf) {
        try { Remove-Item -LiteralPath $path -Force; return 0 } catch { Warn "삭제 실패: $Rel"; return 1 }
    } elseif (Test-Path -LiteralPath $path) {
        Warn "파일이 아니라서 삭제하지 않았습니다: $Rel"
    }
    return 2
}

function Cmd-Apply {
    Require-Backup
    Verify-Package
    Acquire-Lock
    $ok = 0; $same = 0; $fail = 0; $removed = 0; $pruned = 0
    $rc = Run-Hook "pre_apply"
    if ($rc -eq 1) { Log-History "중단: pre_apply 실패"; Die "pre_apply 명령이 실패해서 apply 를 중단했습니다. 서버 파일은 바뀌지 않았습니다." }
    if ($rc -eq 2 -and -not $NoHooks) { Die "pre_apply 명령을 실행하지 않아 apply 를 중단했습니다. 서버 파일은 바뀌지 않았습니다. (명령 없이 진행하려면 -NoHooks)" }
    $script:RecordDirs = $true; $script:CreatedDirs = @()
    foreach ($rel in (Read-List $Manifest)) {
        Check-Rel $rel
        $src = Package-Path $FilesDir $rel
        if (-not (Test-Path -LiteralPath $src -PathType Leaf)) { Warn "패키지에 파일이 없습니다: files/$rel"; $fail++; continue }
        if (Same-File $src (Server-Path $rel)) { $same++; continue }  # 서버와 같은 파일은 건드리지 않는다
        try { Put-File $src $rel; $ok++ } catch { Warn "반영 실패: $rel ($($_.Exception.Message))"; $fail++ }
    }
    $script:RecordDirs = $false
    Write-List (Join-Path $StateDir "created_dirs.txt") $script:CreatedDirs
    foreach ($rel in (Read-List $Deletes)) {
        $r = Remove-ServerFile $rel
        if ($r -eq 0) { $removed++ } elseif ($r -eq 1) { $fail++ }
    }
    foreach ($rel in (Read-List (Join-Path $StateDir "pruned.txt"))) {
        $r = Remove-ServerFile $rel
        if ($r -eq 0) { $pruned++ } elseif ($r -eq 1) { $fail++ }
    }
    Write-List (Join-Path $StateDir "apply.done") @((Get-Date -Format "yyyy-MM-dd HH:mm:ss"))
    $summary = "반영 ${ok}개, 동일(건너뜀) ${same}개, 삭제 ${removed}개, 정리 ${pruned}개, 실패 ${fail}개"
    Info $summary
    if ($fail -ne 0) {
        Log-History $summary
        if (Has-Hook "post_apply") { Warn "실패 항목이 있어 post_apply 명령은 실행하지 않았습니다." }
        Warn "실패 항목이 있습니다. 확인 후 필요하면 '$ScriptName rollback' 으로 복구하세요."
        Release-Lock; exit 1
    }
    $rc = Run-Hook "post_apply"
    if ($rc -eq 1) {
        Log-History "$summary, post_apply 실패"
        Warn "파일은 반영됐지만 post_apply 명령이 실패했습니다. 확인 후 필요하면 '$ScriptName rollback' 으로 복구하세요."
        Release-Lock; exit 1
    }
    Log-History $summary
}

function Cmd-Rollback {
    Require-Backup
    Acquire-Lock
    $restored = 0; $removed = 0; $fail = 0
    foreach ($rel in (Read-List (Join-Path $StateDir "existed.txt"))) {
        Check-Rel $rel
        try { Put-File (Package-Path $BackupFiles $rel) $rel; $restored++ } catch { Warn "복구 실패: $rel"; $fail++ }
    }
    foreach ($rel in (Read-List (Join-Path $StateDir "absent.txt"))) {
        $r = Remove-ServerFile $rel
        if ($r -eq 0) { $removed++ } elseif ($r -eq 1) { $fail++ }
    }
    $dirs = Remove-CreatedDirs  # apply 가 새로 만든 폴더가 비었으면 함께 정리
    Remove-Item -LiteralPath (Join-Path $StateDir "apply.done") -Force -ErrorAction SilentlyContinue
    $summary = "복구 ${restored}개, 신규 파일 제거 ${removed}개, 빈 폴더 정리 ${dirs}개, 실패 ${fail}개"
    Info $summary
    if ($fail -ne 0) { Log-History $summary; Release-Lock; exit 1 }
    $rc = Run-Hook "post_rollback"
    if ($rc -eq 1) { Log-History "$summary, post_rollback 실패"; Release-Lock; exit 1 }
    Log-History $summary
}

# ------------------------------------------------------------------ 시작
# 예상하지 못한 오류도 종료 코드 1 과 함께 알리고 잠금을 푼다.
trap {
    Write-Host "[오류] 예상하지 못한 오류: $($_.Exception.Message)"
    Release-Lock
    exit 1
}
if (-not $Command) { Show-Usage }
if ($Command -eq "history") { Cmd-History; exit 0 }

$script:Target = if ($Target) { $Target } else { $TargetDefault }
if (-not (Test-Path -LiteralPath $script:Target -PathType Container)) { Die "대상 경로가 없습니다: $($script:Target)" }
$script:Target = (Resolve-Path -LiteralPath $script:Target).ProviderPath.TrimEnd("\")
if ($script:Target -match '^[A-Za-z]:$') { $script:Target += "\" }
if (-not (Test-Path -LiteralPath $Manifest)) { Die "manifest.txt 가 없습니다. 패키지 폴더 전체를 서버에 올렸는지 확인하세요." }

Info "대상 경로: $($script:Target)"
try {
    switch ($Command) {
        "check"    { Cmd-Check }
        "backup"   { Cmd-Backup }
        "apply"    { Cmd-Apply }
        "rollback" { Cmd-Rollback }
        default    { Show-Usage }
    }
} finally {
    Release-Lock
}
exit 0
