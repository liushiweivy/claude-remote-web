' Claude Session Bridge - logon autostart
' 1) if :8765 is not listening yet, start the service hidden (python, logfile)
' 2) wait for the port (max 15s)
' 3) open the browser page with token
' Pure ASCII. Install: shortcut to this file in shell:startup (wscript.exe).
Option Explicit

Dim sh, fso, proj
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
proj = "E:\1workspace\claude-remote-web"

Function PortUp()
  ' Must match a LISTENING socket. A bare ":8765" substring is unreliable:
  ' TIME_WAIT rows to 127.0.0.1:8765 linger after the service dies
  ' (cpolar keeps opening such rows) and caused a false "already up".
  Dim code
  code = sh.Run("cmd /c netstat -ano -p tcp | findstr /R /C:"":8765 "" | findstr LISTENING", 0, True)
  PortUp = (code = 0)
End Function

If Not PortUp() Then
  sh.Run """" & proj & "\tools\autostart-run.bat""", 0, False
End If

Dim listening, i
listening = False
For i = 1 To 30
  WScript.Sleep 500
  If PortUp() Then
    listening = True
    Exit For
  End If
Next

If listening Then
  Dim token, tokenFile
  token = ""
  tokenFile = proj & "\data\token.txt"
  If fso.FileExists(tokenFile) Then
    token = Trim(fso.OpenTextFile(tokenFile, 1).ReadAll())
  End If
  sh.Run "http://127.0.0.1:8765/?token=" & token, 1, False
End If
