. "$PSScriptRoot\common.ps1"
Assert-ProjectEnvironment
& $ProjectPython -m agent_core.main
Assert-LastCommand 'Starting Agent Core'
