# Install logon-autostart shortcut for the public URL notifier (no admin needed).
$startup = [Environment]::GetFolderPath("Startup")
$lnk = Join-Path $startup "Claude Public URL Notify.lnk"
$s = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk)
$s.TargetPath = "wscript.exe"
$s.Arguments = '"E:\1workspace\claude-remote-web\tools\notify-public-url.vbs"'
$s.WorkingDirectory = "E:\1workspace\claude-remote-web"
$s.Description = "Open+WeChat-notify cpolar public URL at logon"
$s.Save()
Write-Output ("shortcut: " + $lnk)
Write-Output ("exists: " + (Test-Path $lnk))
