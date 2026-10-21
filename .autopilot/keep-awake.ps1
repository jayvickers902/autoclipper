# Keeps Windows from idle-sleeping while the autoclipper autopilot runs.
# autopilot.sh copies this to %TEMP% and starts it; it exits when the flag file
# disappears or after -Hours, whichever comes first. Keep this file ASCII-only.
param([double]$Hours = 10)

$sig = '[DllImport("kernel32.dll")] public static extern uint SetThreadExecutionState(uint esFlags);'
$k = Add-Type -MemberDefinition $sig -Name 'Power' -Namespace 'Autoclipper' -PassThru
$flag = Join-Path $env:TEMP 'autoclipper-awake.flag'
$until = (Get-Date).AddHours($Hours)

# ES_CONTINUOUS | ES_SYSTEM_REQUIRED: no idle sleep while this thread lives.
[void]$k::SetThreadExecutionState([uint32]2147483649)
while ((Test-Path $flag) -and ((Get-Date) -lt $until)) { Start-Sleep -Seconds 60 }
# ES_CONTINUOUS alone clears the request.
[void]$k::SetThreadExecutionState([uint32]2147483648)
