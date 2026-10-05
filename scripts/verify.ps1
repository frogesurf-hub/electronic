. "$PSScriptRoot\common.ps1"
Assert-ProjectEnvironment
& $ProjectPython -m pip check
Assert-LastCommand 'Checking Python dependencies'
& $ProjectPython -m pytest -q src/agent_core/tests/test_settings.py src/agent_core/tests/test_provider_contract.py src/agent_core/tests/test_deepseek_provider.py src/agent_core/tests/test_websocket_server.py src/agent_core/tests/test_main.py
Assert-LastCommand 'Checking the migrated environment and communication boundaries'
Assert-ProjectDotnet
& $ProjectDotnet build $DesktopProject --nologo -v minimal
Assert-LastCommand 'Building the WPF desktop'
