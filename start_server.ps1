# ── MiseOJeu Analyzer — démarrage automatique ─────────────────────────────────
# Ce script s'assure que Flask tourne en permanence.
# Relance automatiquement le serveur s'il s'arrête.

$ProjectDir = "C:\Users\DaveTardif\Documents\Claude\miseojeu-analyzer"
$Python     = "$ProjectDir\venv\Scripts\python.exe"
$App        = "$ProjectDir\app.py"
$LogFile    = "$ProjectDir\server.log"
$PidFile    = "$ProjectDir\server.pid"
$Port       = 5000

Set-Location $ProjectDir

while ($true) {
    # Vérifier si quelque chose tourne déjà sur le port 5000
    $existing = Get-NetTCPConnection -LocalPort $Port -ErrorAction SilentlyContinue
    if ($existing) {
        $pid5000 = $existing.OwningProcess | Select-Object -First 1
        $proc    = Get-Process -Id $pid5000 -ErrorAction SilentlyContinue
        if ($proc -and ($proc.Name -like "python*")) {
            # Flask déjà en marche — attendre et vérifier de nouveau
            Start-Sleep -Seconds 30
            continue
        } else {
            # Port occupé par autre chose — libérer
            Stop-Process -Id $pid5000 -Force -ErrorAction SilentlyContinue
            Start-Sleep -Seconds 2
        }
    }

    # Démarrer Flask
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -Path $LogFile -Value "[$timestamp] Démarrage de Flask..."

    $proc = Start-Process -FilePath $Python `
                          -ArgumentList $App `
                          -WorkingDirectory $ProjectDir `
                          -RedirectStandardOutput $LogFile `
                          -RedirectStandardError  "$ProjectDir\server_err.log" `
                          -NoNewWindow `
                          -PassThru

    $proc.Id | Out-File -FilePath $PidFile -Encoding ascii

    Add-Content -Path $LogFile -Value "[$timestamp] Flask démarré (PID $($proc.Id))"

    # Attendre que le processus se termine (crash ou arrêt volontaire)
    $proc.WaitForExit()

    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -Path $LogFile -Value "[$timestamp] Flask arrêté (code $($proc.ExitCode)) — redémarrage dans 10s..."
    Start-Sleep -Seconds 10
}
