# Install logon-autostart shortcut for Claude Session Bridge (no admin needed).
# Visible mode: cmd window stays open showing the service console.
$startup = [Environment]::GetFolderPath("Startup")
$lnk = Join-Path $startup "Claude Session Bridge.lnk"
$s = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk)
$s.TargetPath = "$env:SystemRoot\System32\cmd.exe"
$s.Arguments = '/k "E:\1workspace\claude-remote-web\tools\autostart-visible.bat"'
$s.WorkingDirectory = "E:\1workspace\claude-remote-web"
$s.WindowStyle = 1
$s.Description = "Claude Session Bridge visible autostart"
$s.Save()
Write-Output ("shortcut: " + $lnk)
Write-Output ("exists: " + (Test-Path $lnk))
