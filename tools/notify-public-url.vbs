' Claude Session Bridge - public URL notifier (logon autostart, hidden)
' Waits for the cpolar web8765 tunnel, opens the public HTTP URL in the
' browser and sends it to WeChat File Transfer (wxauto lives in the
' wechat-claude-bridge venv, so we use that pythonw).
' Pure ASCII. Install: shortcut to this file in shell:startup (wscript.exe).
Option Explicit

Dim sh
Set sh = CreateObject("WScript.Shell")

' Give network + cpolar service a head start; the script itself polls too.
WScript.Sleep 30000

sh.Run """E:\1workspace\wechat-claude-bridge\.venv\Scripts\pythonw.exe"" ""E:\1workspace\claude-remote-web\tools\notify_public_url.py""", 0, False
