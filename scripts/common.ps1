$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$ProjectPython = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$ProjectDotnet = Join-Path $ProjectRoot '.tools\dotnet\dotnet.exe'
$DesktopProject = Join-Path $ProjectRoot 'src\Desktop\DesktopCompanion.Desktop\DesktopCompanion.Desktop\DesktopCompanion.Desktop.csproj'

Set-Location -LiteralPath $ProjectRoot
$env:DOTNET_CLI_TELEMETRY_OPTOUT = '1'
$env:DOTNET_NOLOGO = '1'
$env:DOTNET_CLI_HOME = Join-Path $ProjectRoot '.cache\dotnet-home'
$env:NUGET_PACKAGES = Join-Path $ProjectRoot '.cache\nuget'
if (Test-Path -LiteralPath $ProjectDotnet) {
    $env:DOTNET_ROOT = Split-Path -Parent $ProjectDotnet
} else {
    $ProjectDotnet = 'dotnet'
}

function Assert-ProjectEnvironment {
    if (-not (Test-Path -LiteralPath $ProjectPython)) {
        throw 'Run scripts\setup.ps1 first to prepare the project environment.'
    }
}

function Assert-ProjectDotnet {
    try {
        $SdkVersion = & $ProjectDotnet --version 2>$null
        $SdkStatus = $LASTEXITCODE
    } catch {
        $SdkStatus = 1
    }
    if ($SdkStatus -ne 0 -or $SdkVersion -notmatch '^8\.0\.') {
        throw 'The .NET 8 SDK is not ready. Run setup.ps1 or install the verified offline SDK before building the desktop.'
    }
}

function Assert-LastCommand {
    param([string]$Action)
    if ($LASTEXITCODE -ne 0) {
        throw "$Action failed (exit code $LASTEXITCODE)."
    }
}
