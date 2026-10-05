param(
    [string]$Manifest,
    [string]$Target,
    [string]$Verify,
    [switch]$Activate,
    [string]$UseCollection,
    [switch]$CheckOnly,
    [ValidateSet('auto', 'always', 'never')][string]$Ocr = 'auto',
    [string]$Language = 'zh-Hans-CN',
    [int]$MaxChars = 1200
)
. "$PSScriptRoot\common.ps1"
if ($UseCollection -and ($Manifest -or $Target -or $Verify -or $CheckOnly -or $Activate)) {
    throw 'UseCollection is a separate operation. Do not combine it with import options.'
}
if ($Verify -and ($Manifest -or $Target -or $CheckOnly -or $Activate)) {
    throw 'Verify is a separate operation. Pass the collection folder directly.'
}
$RuntimeConfig = Join-Path $ProjectRoot '.local\document-import-runtime.json'
if (-not (Test-Path -LiteralPath $RuntimeConfig)) {
    throw 'Run scripts\setup-document-tools.ps1 first.'
}
$DocumentPython = (Get-Content -LiteralPath $RuntimeConfig -Raw | ConvertFrom-Json).python
if (-not (Test-Path -LiteralPath $DocumentPython -PathType Leaf)) {
    throw 'Configured document Python is unavailable. Run setup-document-tools.ps1 again.'
}
$ImportArguments = @((Join-Path $PSScriptRoot 'prepare_documents.py'))
if ($UseCollection) {
    $ImportArguments += @('--activate-collection', $UseCollection)
} elseif ($Verify) {
    $ImportArguments += @('--verify', $Verify)
} else {
    if (-not $Manifest) { throw 'Pass -Manifest with the input manifest.json path.' }
    $ImportArguments += @('--manifest', $Manifest, '--ocr', $Ocr, '--language', $Language,
        '--max-chars', [string]$MaxChars)
    if ($Target) { $ImportArguments += @('--target', $Target) }
    if ($CheckOnly) { $ImportArguments += '--check-only' }
    if ($Activate) {
        if ($CheckOnly) { throw 'CheckOnly cannot activate a collection.' }
        $ImportArguments += '--activate'
    }
}
& $DocumentPython @ImportArguments
Assert-LastCommand 'Preparing documents'
