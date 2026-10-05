. "$PSScriptRoot\common.ps1"
Assert-ProjectEnvironment
$ProviderReady = & $ProjectPython -c "from agent_core.config import get_settings; s=get_settings(); print('ready' if s.deepseek_api_key and s.deepseek_api_key.get_secret_value().strip() else 'missing')"
Assert-LastCommand 'Checking the local model configuration'
if ($ProviderReady.Trim() -ne 'ready') {
    throw 'Fill in DCA_DEEPSEEK_API_KEY in the project .env file, then run start-core.ps1 again. Keep the key in your local configuration.'
}
& $ProjectPython -m agent_core.main
Assert-LastCommand 'Starting Agent Core'
