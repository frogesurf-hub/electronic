. "$PSScriptRoot\common.ps1"
Assert-ProjectEnvironment
Assert-ProjectDotnet
& $ProjectDotnet run --project $DesktopProject
Assert-LastCommand 'Starting the WPF desktop'
