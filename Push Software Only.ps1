param(
    [Parameter(Mandatory = $true)]
    [string]$Message,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $projectRoot

if (-not (Test-Path -LiteralPath ".git")) {
    throw "This script must stay in the root of the Plant Production Git repository."
}

$allowedExtensions = @(
    ".py", ".html", ".css", ".js", ".gs", ".md", ".txt", ".yaml", ".yml", ".bat", ".ps1"
)
$allowedNames = @(".gitignore", ".env.example")

function Test-ForbiddenPath([string]$Path) {
    $normalized = $Path.Replace("\", "/")
    if ($normalized -eq ".env.example") { return $false }
    return $normalized -match "(?i)(^|/)(\.env($|\.)|\.venv(/|$)|__pycache__(/|$)|data(/|$)|backups?(/|$)|logs?(/|$)|credentials?(/|$)|secrets?(/|$))" -or
           $normalized -match "(?i)\.(db|db-journal|sqlite|sqlite3|bak|dump|log|pem|key|p12|pfx)$" -or
           $normalized -match "(?i)(plant_production_before_|supabase_before_recovery_)"
}

function Assert-NoTrackedData {
    $blocked = @()
    foreach ($file in @(git ls-files)) {
        if (Test-ForbiddenPath $file) { $blocked += $file }
    }
    if ($blocked.Count -gt 0) {
        throw "Blocked: data or credential files are already tracked:`n$($blocked -join "`n")"
    }
}

function Assert-AllowedSoftwareFile([string]$Path) {
    if (Test-ForbiddenPath $Path) {
        throw "Blocked local data or credential file: $Path"
    }
    $name = [System.IO.Path]::GetFileName($Path)
    $extension = [System.IO.Path]::GetExtension($Path).ToLowerInvariant()
    if ($allowedNames -notcontains $name -and $allowedExtensions -notcontains $extension) {
        throw "Blocked unapproved file type: $Path"
    }
}

function Assert-NoSecretsInStagedDiff {
    $addedLines = @(git diff --cached --no-color --unified=0) |
        Where-Object { $_ -match '^\+' -and $_ -notmatch '^\+\+\+' }

    $secretSignatures = @(
        "postgres(?:ql)?://[^\s:]+:[^\s@]+@",
        "GOCSPX-[A-Za-z0-9_-]+",
        "1//[A-Za-z0-9_-]+",
        "AIza[0-9A-Za-z_-]{30,}",
        "github_pat_[A-Za-z0-9_]+",
        "ghp_[A-Za-z0-9]{20,}",
        "-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----"
    )
    foreach ($line in $addedLines) {
        foreach ($pattern in $secretSignatures) {
            if ($line -match $pattern) {
                throw "Blocked: a possible secret was found in the staged changes."
            }
        }

        if ($line -match '^\+\s*(SUPABASE_DATABASE_URL|DATABASE_URL|GOOGLE_APPS_SCRIPT_SECRET|GMAIL_APP_PASSWORD|APP_PASSWORD)=([^\r\n]*)$') {
            $value = $Matches[2].Trim().Trim('"').Trim("'")
            $placeholder = $value -eq "" -or $value -match "(?i)^(your-|the-|choose-|placeholder|example|<)"
            if (-not $placeholder) {
                throw "Blocked: a real-looking value for $($Matches[1]) was found in staged changes."
            }
        }
    }
}

Assert-NoTrackedData

$candidates = @(
    git ls-files --modified --others --exclude-standard |
        Where-Object { $_ -and $_.Trim() }
)

foreach ($file in $candidates) {
    Assert-AllowedSoftwareFile $file
    git add -- $file
    if ($LASTEXITCODE -ne 0) { throw "Git could not stage: $file" }
}

$staged = @(git diff --cached --name-only | Where-Object { $_ -and $_.Trim() })
foreach ($file in $staged) { Assert-AllowedSoftwareFile $file }
Assert-NoSecretsInStagedDiff

if ($staged.Count -gt 0) {
    Write-Host "Software files ready to commit:" -ForegroundColor Green
    $staged | ForEach-Object { Write-Host "  $_" }
} else {
    Write-Host "No new software changes need to be committed."
}

$ahead = [int](git rev-list --count origin/main..HEAD)
if ($DryRun) {
    Write-Host "Dry run complete. Nothing was committed or pushed." -ForegroundColor Yellow
    Write-Host "Local commits waiting to push: $ahead"
    exit 0
}

if ($staged.Count -gt 0) {
    git commit -m $Message
    if ($LASTEXITCODE -ne 0) { throw "Git commit failed. Nothing was pushed." }
    $ahead = [int](git rev-list --count origin/main..HEAD)
}

if ($ahead -eq 0) {
    Write-Host "There are no local commits waiting to push."
    exit 0
}

Write-Host "Local commits waiting to push: $ahead" -ForegroundColor Cyan
$confirmation = Read-Host "Type PUSH to upload SOFTWARE ONLY to GitHub"
if ($confirmation -cne "PUSH") {
    Write-Host "Cancelled. Nothing was pushed." -ForegroundColor Yellow
    exit 0
}

git push origin main
if ($LASTEXITCODE -ne 0) { throw "Git push failed. Your commit is still safe locally." }
Write-Host "Software-only push completed." -ForegroundColor Green
