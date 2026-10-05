param([switch]$SkipDotnet)
. "$PSScriptRoot\common.ps1"

if (-not (Test-Path -LiteralPath $ProjectPython)) {
    & py -3.11 -m venv (Join-Path $ProjectRoot '.venv')
    Assert-LastCommand 'Creating the Python 3.11 environment'
}
& $ProjectPython -c "import sys; assert sys.version_info[:2] == (3, 11), 'Python 3.11 is required'"
Assert-LastCommand 'Checking Python version'

$PreviousNoIndex = [Environment]::GetEnvironmentVariable('PIP_NO_INDEX', 'Process')
try {
    $env:PIP_NO_INDEX = '0'
    & $ProjectPython -m pip install --no-cache-dir --only-binary=:all: --index-url https://pypi.org/simple --disable-pip-version-check -r (Join-Path $ProjectRoot 'requirements-dev.lock')
    Assert-LastCommand 'Installing the locked Python dependencies'
    & $ProjectPython -m pip install --no-cache-dir --disable-pip-version-check --no-build-isolation --no-deps -e $ProjectRoot
    Assert-LastCommand 'Installing the project'
} finally {
    [Environment]::SetEnvironmentVariable('PIP_NO_INDEX', $PreviousNoIndex, 'Process')
}

if (-not (Test-Path -LiteralPath (Join-Path $ProjectRoot '.env'))) {
    Copy-Item -LiteralPath (Join-Path $ProjectRoot '.env.example') -Destination (Join-Path $ProjectRoot '.env')
}

if (-not $SkipDotnet) {
    & $ProjectPython (Join-Path $PSScriptRoot 'download_dotnet.py')
    Assert-LastCommand 'Preparing the official .NET SDK'
}
Write-Host 'Environment ready. Fill in your local .env before using the real model.'
