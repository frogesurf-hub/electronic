. "$PSScriptRoot\common.ps1"
Assert-ProjectEnvironment
& $ProjectDotnet run --project $DesktopProject
Assert-LastCommand 'Starting the WPF desktop'
