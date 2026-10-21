# Registers a Windows scheduled task that runs run_daily.ps1 every hour from 08:00 to 19:00.
# If the laptop is off or asleep at a scheduled time, it runs once as soon as it's back on.
# Run once from the repo folder:  .\scripts\install_task.ps1   (re-running replaces the old task)
$script = Join-Path (Split-Path $PSScriptRoot -Parent) "run_daily.ps1"

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
            -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$script`""

# Start at 08:00 each day, repeat hourly for 11 hours (08:00, 09:00 ... 19:00).
# The extra minute makes sure the 19:00 run is inside the window.
$trigger = New-ScheduledTaskTrigger -Daily -At 8am
$hourly  = New-ScheduledTaskTrigger -Once -At 8am `
             -RepetitionInterval (New-TimeSpan -Hours 1) `
             -RepetitionDuration (New-TimeSpan -Hours 11 -Minutes 1)
$trigger.Repetition = $hourly.Repetition

$settings = New-ScheduledTaskSettingsSet `
              -StartWhenAvailable `
              -MultipleInstances IgnoreNew `
              -ExecutionTimeLimit (New-TimeSpan -Minutes 50) `
              -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

Register-ScheduledTask -TaskName "job-radar" -Action $action -Trigger $trigger `
  -Settings $settings -Description "Hourly Irish entry-level job digest, 08:00-19:00" -Force
Write-Host "Registered. Runs hourly 08:00-19:00. Test now with:  Start-ScheduledTask -TaskName job-radar"