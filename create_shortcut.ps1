$desktop = [Environment]::GetFolderPath('Desktop')
$WshShell = New-Object -ComObject WScript.Shell
$Shortcut = $WshShell.CreateShortcut("$desktop\DaveBet NHL.lnk")
$Shortcut.TargetPath = 'C:\Users\DaveTardif\Documents\Claude\miseojeu-analyzer\start.bat'
$Shortcut.WorkingDirectory = 'C:\Users\DaveTardif\Documents\Claude\miseojeu-analyzer'
$Shortcut.Description = 'DaveBet - App NHL'
$Shortcut.Save()
Write-Host "Raccourci cree sur le bureau: $desktop\DaveBet NHL.lnk"
