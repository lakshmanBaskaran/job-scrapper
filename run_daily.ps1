# Daily run on this machine, using the local Ollama model.
# Task Scheduler calls this; you can also run it by hand: .\run_daily.ps1
# -u = unbuffered, so radar.log shows progress even if a run is cut off.
Set-Location $PSScriptRoot

$env:RADAR_LLM = "ollama"
if (-not $env:OLLAMA_MODEL) { $env:OLLAMA_MODEL = "qwen2.5:7b" }
# SMTP_USER, SMTP_PASSWORD and MAIL_TO come from the .env file next to this script.

"`n===== $(Get-Date -Format 'yyyy-MM-dd HH:mm') =====" | Out-File -Append -Encoding utf8 radar.log
python -u -m radar.main 2>&1 | Out-File -Append -Encoding utf8 radar.log