$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-WindowStyle Hidden -ExecutionPolicy Bypass -File `"C:\Users\DaveTardif\Documents\Claude\miseojeu-analyzer\start_server.ps1`"" `
    -WorkingDirectory "C:\Users\DaveTardif\Documents\Claude\miseojeu-analyzer"

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 5 `
    -RestartInterval (New-TimeSpan -Minutes 2) `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew

$principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Highest

Register-ScheduledTask `
    -TaskName "MiseOJeuAnalyzer" `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Demarre automatiquement le serveur Flask MiseOJeu Analyzer" `
    -Force

Write-Host "Tache planifiee creee avec succes" -ForegroundColor Green
Write-Host "Le serveur demarrera automatiquement a chaque connexion Windows." -ForegroundColor Cyan
