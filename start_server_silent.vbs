' Lance start_server.ps1 en arrière-plan sans fenêtre visible
Dim shell
Set shell = CreateObject("WScript.Shell")
shell.Run "powershell.exe -WindowStyle Hidden -ExecutionPolicy Bypass -File ""C:\Users\DaveTardif\Documents\Claude\miseojeu-analyzer\start_server.ps1""", 0, False
Set shell = Nothing
