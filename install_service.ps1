# =====================================================================================
#  install_service.ps1 — register the Autonomous AI Agent as an auto-start Windows service
#  Run this in an ELEVATED PowerShell:  right-click PowerShell  ->  "Run as administrator"
#  then:   & "D:\IT Dept\Developements\AI Agent\Autonomous Agent\autonomous-agent v1.1\install_service.ps1"
# =====================================================================================
$ErrorActionPreference = "Stop"

# --- must be elevated (creating a service needs admin) ---
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
           ).IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "ERROR: run this in an ELEVATED PowerShell (right-click > Run as administrator)." -ForegroundColor Red
    exit 1
}

# --- paths (verified this session) ---
$dir  = "D:\IT Dept\Developements\AI Agent\Autonomous Agent\autonomous-agent v1.1"
$nssm = Join-Path $dir "tools\nssm.exe"
$py   = "C:\Users\<USER>\AppData\Local\Python\pythoncore-3.14-64\python.exe"   # REAL python, not the WindowsApps alias
$svc  = "AI-Agent"
$logs = Join-Path $dir "logs"
New-Item -ItemType Directory -Force $logs | Out-Null
foreach ($p in @($nssm, $py, (Join-Path $dir "main.py"))) {
    if (-not (Test-Path $p)) { Write-Host "MISSING: $p" -ForegroundColor Red; exit 1 }
}

# --- password so the service runs AS you (needed: your Claude login + Gmail token live in your profile) ---
Write-Host "The service runs AS user '.\hp' so it can use your Claude login. Enter your Windows password." -ForegroundColor Cyan
$cred  = Get-Credential -UserName ".\hp" -Message "Windows password for .\hp (the account the agent runs as)"
$plain = $cred.GetNetworkCredential().Password

# --- clean any previous install (idempotent) ---
if (Get-Service $svc -ErrorAction SilentlyContinue) {
    Write-Host "Removing existing $svc ..." -ForegroundColor Yellow
    & $nssm stop   $svc 2>$null | Out-Null
    & $nssm remove $svc confirm | Out-Null
    Start-Sleep -Seconds 2
}

# --- install + configure ---
& $nssm install $svc $py "main.py"
& $nssm set $svc AppDirectory $dir
& $nssm set $svc AppEnvironmentExtra "PATH=C:\Users\<USER>\.local\bin;%PATH%" "PYTHONUTF8=1" "PYTHONIOENCODING=utf-8"
& $nssm set $svc ObjectName ".\hp" $plain
& $nssm set $svc Start SERVICE_AUTO_START          # start automatically on boot
& $nssm set $svc AppExit Default Restart           # auto-restart if it ever crashes
& $nssm set $svc AppThrottle 10000                 # wait 10s between restart attempts
& $nssm set $svc AppStdout (Join-Path $logs "service_out.log")
& $nssm set $svc AppStderr (Join-Path $logs "service_err.log")
& $nssm set $svc DisplayName "Autonomous Sales Agent"
& $nssm set $svc Description "24/7 autonomous sales agent (intl.sales@) - sourcing/quoting; dashboard http://127.0.0.1:8002"

# --- keep the laptop awake: closing the lid does nothing while on AC power ---
powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0
powercfg /setactive SCHEME_CURRENT

# --- start it ---
& $nssm start $svc
Start-Sleep -Seconds 6

Write-Host "`n================ RESULT ================" -ForegroundColor Green
& $nssm status $svc
Get-Service $svc | Select-Object Name, Status, StartType | Format-Table -AutoSize
Write-Host "Dashboard : http://127.0.0.1:8002"
Write-Host "Live log  : Get-Content `"$logs\autonomous.log`" -Tail 40 -Wait"
Write-Host ""
Write-Host "VERIFY the log shows 'Authenticated as intl.sales@...' and 'Claude Code SDK connected'." -ForegroundColor Cyan
Write-Host "If you instead see 'Claude Code CLI not found' or repeated restarts, the Claude CLI isn't"
Write-Host "working under the service account - tell your assistant and switch to the Task Scheduler fallback."
