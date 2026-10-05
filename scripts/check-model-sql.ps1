. "$PSScriptRoot\common.ps1"
Assert-ProjectEnvironment
& $ProjectPython "$PSScriptRoot\check_model_sql.py"
Assert-LastCommand 'Checking DeepSeek SQL generation and the reference result'
