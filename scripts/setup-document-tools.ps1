param([string]$PythonPath)
. "$PSScriptRoot\common.ps1"

if (-not $PythonPath) {
    $BundledPython = Join-Path ([Environment]::GetFolderPath('UserProfile')) '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
    $Candidates = @($ProjectPython, $BundledPython)
} else {
    $Candidates = @($PythonPath)
}
$SelectedPython = $null
foreach ($Candidate in $Candidates) {
    if (-not (Test-Path -LiteralPath $Candidate -PathType Leaf)) { continue }
    & $Candidate -c "import importlib.util, sys; sys.exit(0 if all(importlib.util.find_spec(m) for m in ['pdfplumber', 'pypdfium2', 'PIL']) else 1)"
    if ($LASTEXITCODE -eq 0) { $SelectedPython = (Resolve-Path -LiteralPath $Candidate).Path; break }
}
if (-not $SelectedPython) {
    throw 'PDF tools unavailable. Prepare a Python environment using requirements-documents.txt, then pass -PythonPath.'
}
$RuntimeDirectory = Join-Path $ProjectRoot '.local'
New-Item -ItemType Directory -Force -Path $RuntimeDirectory | Out-Null
@{python=$SelectedPython} | ConvertTo-Json | Set-Content -LiteralPath (
    Join-Path $RuntimeDirectory 'document-import-runtime.json') -Encoding UTF8
Write-Host 'Local document tools configured. No network download or model call was used.'
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (
    Join-Path $PSScriptRoot 'ocr-image.ps1') -ListLanguages
Assert-LastCommand 'Checking Windows OCR languages'
