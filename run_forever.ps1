# Keeps the live bot running until a STOP file exists in this folder.
#   start (detached, survives closing Claude Code / the terminal):
#     Start-Process powershell -WindowStyle Minimized -ArgumentList '-ExecutionPolicy','Bypass','-File','run_forever.ps1'
#   stop:  create a file named STOP in this folder. The bot cancels its resting orders and exits; no restart.
# A crash or network outage restarts the bot after 30 s. A halt writes STOP, so it does not restart.
Set-Location $PSScriptRoot
$env:SUSQ_API_KEY = [Environment]::GetEnvironmentVariable('SUSQ_API_KEY', 'User')
$env:PYTHONIOENCODING = 'utf-8'
while (-not (Test-Path STOP)) {
    $log = "logs\live-$(Get-Date -Format yyyyMMdd).txt"
    cmd /c "echo === start %date% %time% === >> $log"
    cmd /c "python -u execute.py --live >> $log 2>&1"
    cmd /c "echo === bot exited (code %errorlevel%) %date% %time% === >> $log"
    if (Test-Path STOP) { break }
    Start-Sleep -Seconds 30
}
$log = "logs\live-$(Get-Date -Format yyyyMMdd).txt"
cmd /c "echo === supervisor stopped %date% %time% (STOP file present) === >> $log"
