Write-Host ""
Write-Host "==================================================" -ForegroundColor Cyan
Write-Host "  PeopleLink — Developer Setup" -ForegroundColor Cyan
Write-Host "==================================================" -ForegroundColor Cyan
Write-Host ""

function Step($n, $msg) { Write-Host "[$n] $msg..." -ForegroundColor Yellow }
function Ok($msg)        { Write-Host "    OK: $msg" -ForegroundColor Green }
function Skip($msg)      { Write-Host "    Already installed: $msg" -ForegroundColor Gray }
function Fail($msg)      { Write-Host "    ERROR: $msg" -ForegroundColor Red; exit 1 }

function Refresh-Path {
    $env:PATH = [System.Environment]::GetEnvironmentVariable("PATH","Machine") + ";" +
                [System.Environment]::GetEnvironmentVariable("PATH","User")
}

# ── 1. Git ───────────────────────────────────────────────────────────────────
Step 1 "Checking Git"
if (Get-Command git -ErrorAction SilentlyContinue) {
    Skip "Git $(git --version)"
} else {
    winget install Git.Git --silent --accept-package-agreements --accept-source-agreements | Out-Null
    if ($LASTEXITCODE -ne 0) { Fail "Git install failed — install manually from git-scm.com then re-run" }
    Refresh-Path
    Ok "Git installed"
}

# ── 2. Node.js (needed for Claude Code) ─────────────────────────────────────
Step 2 "Checking Node.js"
if (Get-Command node -ErrorAction SilentlyContinue) {
    Skip "Node.js $(node --version)"
} else {
    winget install OpenJS.NodeJS.LTS --silent --accept-package-agreements --accept-source-agreements | Out-Null
    if ($LASTEXITCODE -ne 0) { Fail "Node.js install failed — install manually from nodejs.org then re-run" }
    Refresh-Path
    Ok "Node.js installed"
}

# ── 3. Claude Code ───────────────────────────────────────────────────────────
Step 3 "Checking Claude Code"
if (Get-Command claude -ErrorAction SilentlyContinue) {
    Skip "Claude Code"
} else {
    Write-Host "    Installing Claude Code..." -ForegroundColor Gray
    npm install -g @anthropic-ai/claude-code 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { Fail "Claude Code install failed" }
    Ok "Claude Code installed"
}

# ── 4. Railway CLI ───────────────────────────────────────────────────────────
Step 4 "Checking Railway CLI"
$railwayCmd = Get-Command railway -ErrorAction SilentlyContinue
if (-not $railwayCmd) {
    $railwayLocal = "$env:LOCALAPPDATA\railway\railway.exe"
    if (Test-Path $railwayLocal) { $railwayCmd = $railwayLocal }
}
if ($railwayCmd) {
    Skip "Railway CLI"
} else {
    Write-Host "    Installing Railway CLI..." -ForegroundColor Gray
    iwr https://railway.com/install.ps1 | iex
    Refresh-Path
    Ok "Railway CLI installed"
}

# ── 5. Clone repo ────────────────────────────────────────────────────────────
Step 5 "Cloning PeopleLink repo"
$repoPath = "$HOME\peoplelink-timesheets"
if (Test-Path $repoPath) {
    Skip "Repo already at $repoPath — pulling latest"
    Set-Location $repoPath
    git pull origin master 2>&1 | Out-Null
    Ok "Up to date"
} else {
    git clone https://github.com/People-Link-LT/peoplelink-timesheets.git $repoPath 2>&1
    if ($LASTEXITCODE -ne 0) { Fail "Clone failed — make sure your GitHub invite has been accepted" }
    Set-Location $repoPath
    Ok "Cloned to $repoPath"
}

# ── 6. Git identity ──────────────────────────────────────────────────────────
Step 6 "Git identity"
if (-not (git config --global user.name 2>&1)) {
    $name = Read-Host "    Your full name"
    git config --global user.name $name
}
if (-not (git config --global user.email 2>&1)) {
    $email = Read-Host "    Your work email"
    git config --global user.email $email
}
Ok "$(git config --global user.name) <$(git config --global user.email)>"

# ── 7. Railway login + link ──────────────────────────────────────────────────
Step 7 "Railway login"
$railway = if (Get-Command railway -ErrorAction SilentlyContinue) { "railway" } else { "$env:LOCALAPPDATA\railway\railway.exe" }
$whoami = & $railway whoami 2>&1
if ($whoami -match "@") {
    Skip "Already logged in as $whoami"
} else {
    Write-Host "    A browser window will open — log in with your Railway account..." -ForegroundColor Gray
    & $railway login
}

Write-Host "    Linking to PeopleLink project..." -ForegroundColor Gray
& $railway link --project ravishing-spontaneity 2>&1 | Out-Null
Ok "Linked to Railway project"

# ── Done ─────────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "==================================================" -ForegroundColor Cyan
Write-Host "  All set! How to work:" -ForegroundColor Cyan
Write-Host "==================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "  1. Open a terminal in: $repoPath" -ForegroundColor White
Write-Host "  2. Run: claude" -ForegroundColor White
Write-Host "  3. Tell Claude what to build or fix" -ForegroundColor White
Write-Host "  4. Claude commits the code — then deploy:" -ForegroundColor White
Write-Host ""
Write-Host "     railway up --detach --service peoplelink-timesheets" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Live site: https://peoplelink-timesheets-production.up.railway.app" -ForegroundColor Cyan
Write-Host "  Repo:      https://github.com/People-Link-LT/peoplelink-timesheets" -ForegroundColor Cyan
Write-Host ""
