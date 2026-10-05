. "$PSScriptRoot\common.ps1"
Assert-ProjectEnvironment
& $ProjectPython -m pip check
Assert-LastCommand 'Checking SQL dependencies'

$SqlTests = @(
    'tests/test_detail_generation_service.py',
    'tests/test_detail_llm_generator.py',
    'tests/test_detail_validator.py',
    'tests/test_database_source_binding.py',
    'tests/test_chinook_pipeline.py',
    'tests/test_readonly_generation_service.py',
    'tests/test_chinook_evaluation_dataset.py',
    'tests/test_evaluation_integration.py'
)
& $ProjectPython -m pytest -q @SqlTests
Assert-LastCommand 'Checking the imported SQL boundaries'

& $ProjectPython "$PSScriptRoot\check_sql.py"
Assert-LastCommand 'Checking offline SQL generation and validation'
