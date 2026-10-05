param([switch]$CheckOnly)

. "$PSScriptRoot\common.ps1"
Assert-ProjectEnvironment
$ImportArguments = @('-m', 'agent_core.retrieval.documents')
if ($CheckOnly) {
    $ImportArguments += '--check-only'
}
& $ProjectPython @ImportArguments
Assert-LastCommand 'Importing demo documents'
