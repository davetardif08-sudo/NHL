' Lance le serveur Flask de Mise-O-Jeu en arriere-plan (sans fenetre)
Dim WshShell
Set WshShell = CreateObject("WScript.Shell")
WshShell.Run """C:\Users\DaveTardif\Documents\Claude\miseojeu-analyzer\venv\Scripts\pythonw.exe"" ""C:\Users\DaveTardif\Documents\Claude\miseojeu-analyzer\app.py""", 0, False
Set WshShell = Nothing
