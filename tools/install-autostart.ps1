# Install logon-autostart shortcut for Claude Session Bridge (no admin needed).
$startup = [Environment]::GetFolderPath("Startup")
$lnk = Join-Path $startup "Claude Session Bridge.lnk"
$s = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk)
$s.TargetPath = "wscript.exe"
$s.Arguments = '"E:\1workspace\claude-remote-web\tools\autostart.vbs"'
$s.WorkingDirectory = "E:\1workspace\claude-remote-web"
$s.Description = "Claude Session Bridge hidden autostart"
$s.Save()
Write-Output ("shortcut: " + $lnk)
Write-Output ("exists: " + (Test-Path $lnk))
