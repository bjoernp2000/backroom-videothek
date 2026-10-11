$startup = [Environment]::GetFolderPath('Startup')
$lnk = Join-Path $startup 'Videothek.lnk'
$ws = New-Object -ComObject WScript.Shell
$sc = $ws.CreateShortcut($lnk)
$sc.TargetPath = 'F:\Projekte\Videothek\Videothek.exe'
$sc.Arguments = ''
$sc.WorkingDirectory = 'F:\Projekte\Videothek'
$sc.IconLocation = 'F:\Projekte\Videothek\assets\videothek.ico'
$sc.Description = 'Videothek als App starten'
$sc.Save()
if (Test-Path $lnk) { Write-Output "OK: $lnk" } else { Write-Output "FEHLER: nicht angelegt" }
