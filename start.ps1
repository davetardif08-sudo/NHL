$appDir  = "C:\Users\DaveTardif\Documents\Claude\miseojeu-analyzer"
$python  = "$appDir\venv\Scripts\python.exe"
$appFile = "$appDir\app.py"

Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  DaveBet - App NHL" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan

Write-Host "Demarrage du serveur..." -ForegroundColor Yellow
Start-Process -FilePath $python -ArgumentList $appFile -WorkingDirectory $appDir -WindowStyle Normal

Write-Host "Attente du serveur (12 secondes)..." -ForegroundColor Yellow
Start-Sleep -Seconds 12

Write-Host "Ouverture du navigateur..." -ForegroundColor Green
Start-Process "http://127.0.0.1:5000"

Write-Host "Serveur en cours. Appuyez sur une touche pour fermer cette fenetre." -ForegroundColor Green
$null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
