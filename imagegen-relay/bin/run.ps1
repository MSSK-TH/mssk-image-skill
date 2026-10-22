# imagegen-relay launcher  --  ASCII only on purpose (no BOM/encoding issues)
#
# Reads config.json from the skill root, injects IMAGE_RELAY_URL / IMAGE_RELAY_KEY,
# then runs the relay CLI with the system Python. This is what makes "copy the
# folder + fill one token" a complete install: no environment variables, no client
# restart, no packages to install.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File run.ps1 generate --prompt "..." --size 1672x941 --out "out.png"

$bin  = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $bin
$cfgPath = Join-Path $root 'config.json'
$cliPath = Join-Path $root 'scripts\image_gen.py'

# Keep non-ASCII output (Chinese error text from the relay, non-ASCII paths) intact:
# Windows PowerShell decodes native-command output with [Console]::OutputEncoding.
try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false) } catch { }
$OutputEncoding = New-Object System.Text.UTF8Encoding($false)

if (-not (Test-Path -LiteralPath $cliPath)) {
    Write-Host "[imagegen-relay] CLI not found: $cliPath"
    exit 4
}

$url = ''
$key = ''

if (Test-Path -LiteralPath $cfgPath) {
    try {
        $cfg = Get-Content -LiteralPath $cfgPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($cfg.api_url) { $url = ([string]$cfg.api_url).Trim() }
        if ($cfg.api_key) { $key = ([string]$cfg.api_key).Trim() }
    } catch {
        Write-Host "[imagegen-relay] config.json is not valid JSON:"
        Write-Host "  $cfgPath"
        Write-Host "  $($_.Exception.Message)"
        exit 2
    }
}

# environment fallback when config.json is missing or empty
if (-not $url -and $env:IMAGE_RELAY_URL)  { $url = $env:IMAGE_RELAY_URL.Trim() }
if (-not $url -and $env:CODEX_API_URL)    { $url = $env:CODEX_API_URL.Trim() }
if (-not $url -and $env:OPENAI_BASE_URL)  { $url = $env:OPENAI_BASE_URL.Trim() }
if (-not $key -and $env:IMAGE_RELAY_KEY)  { $key = $env:IMAGE_RELAY_KEY.Trim() }
if (-not $key -and $env:CODEX_API_KEY)    { $key = $env:CODEX_API_KEY.Trim() }
if (-not $key -and $env:OPENAI_API_KEY)   { $key = $env:OPENAI_API_KEY.Trim() }

if ($url) { $env:IMAGE_RELAY_URL = $url }
if ($key) { $env:IMAGE_RELAY_KEY = $key }

# pick an interpreter: prefer python, fall back to the py launcher
$exe = ''
$pre = @()
$pyCmd = Get-Command python -ErrorAction SilentlyContinue
if ($pyCmd) {
    $exe = $pyCmd.Source
} else {
    $pyLauncher = Get-Command py -ErrorAction SilentlyContinue
    if ($pyLauncher) { $exe = $pyLauncher.Source; $pre = @('-3') }
}

if (-not $exe) {
    Write-Host "[imagegen-relay] Python 3 was not found on PATH."
    Write-Host "  Install Python 3.9+ or make sure 'python' is available in this shell."
    exit 5
}

# Prefer `uv` when it is installed: it supplies Pillow on the fly (needed by --max-dim and
# --exact-size) without installing anything into the user's Python. Without uv we run the
# system interpreter -- every other flag works with the standard library alone.
# Never retry after a non-zero exit: the CLI uses exit codes for real outcomes.
$uvCmd = Get-Command uv -ErrorAction SilentlyContinue
if ($uvCmd) {
    & $uvCmd.Source run --quiet --no-project --with pillow python $cliPath @args
    exit $LASTEXITCODE
}

& $exe @pre $cliPath @args
exit $LASTEXITCODE
