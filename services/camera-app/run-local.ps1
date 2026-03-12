$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir

if (-not $env:RUN_MODE) {
    $env:RUN_MODE = "local"
}

if ($env:PYTHON_BIN) {
    & $env:PYTHON_BIN .\main.py
    exit $LASTEXITCODE
}

if (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3 .\main.py
    exit $LASTEXITCODE
}

if (Get-Command python -ErrorAction SilentlyContinue) {
    & python .\main.py
    exit $LASTEXITCODE
}

throw "Python executable not found. Install Python or add it to PATH."
