. "$PSScriptRoot\common.ps1"
Assert-ProjectEnvironment
& $ProjectPython "$PSScriptRoot\prepare_chinook.py"
Assert-LastCommand 'Preparing the project Chinook database'
