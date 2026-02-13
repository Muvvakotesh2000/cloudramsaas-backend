<powershell>
Set-ExecutionPolicy -ExecutionPolicy Bypass -Scope Process -Force

function Write-EC2Log {
    param ([string]$Message)
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $logMessage = "[$timestamp] $Message"
    Write-Host $logMessage
    Add-Content -Path "C:\CloudRAM\startup.log" -Value $logMessage -ErrorAction SilentlyContinue
}

# Ensure directories exist
New-Item -Path "C:\CloudRAM" -ItemType Directory -Force | Out-Null
New-Item -Path "C:\Users\vm_user\SyncedNotepadFiles" -ItemType Directory -Force | Out-Null
New-Item -Path "C:\CloudRAM\logs" -ItemType Directory -Force | Out-Null

# -----------------------------
# State machine file
# -----------------------------
$stateFile = "C:\CloudRAM\script_state.txt"

function Get-ScriptState {
    if (Test-Path $stateFile) {
        return (Get-Content -Path $stateFile -ErrorAction SilentlyContinue | Select-Object -First 1)
    }
    return "INITIAL"
}

function Set-ScriptState {
    param ([string]$State)
    $State | Out-File -FilePath $stateFile -Force -Encoding ASCII
}

# Create a scheduled task to resume script after reboot
function Register-ResumeTask {
    $taskName = "CloudRAM-ResumeScript"
    $taskAction = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-ExecutionPolicy Bypass -File $PSCommandPath"
    $taskTrigger = New-ScheduledTaskTrigger -AtStartup
    $taskPrincipal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
    Register-ScheduledTask -TaskName $taskName -Action $taskAction -Trigger $taskTrigger -Principal $taskPrincipal -Force -ErrorAction SilentlyContinue | Out-Null
}

# -----------------------------
# ALWAYS-ON services runner (works after Stop/Start too)
# -----------------------------
function Ensure-ServicesScriptsAndTask {
    try {
        $servicesLog = "C:\CloudRAM\logs\services_boot.log"

        $servicesScript = @"
`$ErrorActionPreference = 'Continue'
function L([string]`$m){
  Add-Content -Path '$servicesLog' -Value ("[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), `$m) -ErrorAction SilentlyContinue
}

L "----- startup_services.ps1 begin -----"
Start-Sleep -Seconds 25

# Start UltraVNC service
try {
  `$svc = Get-Service -Name "uvnc_service" -ErrorAction SilentlyContinue
  if (`$svc) {
    if (`$svc.Status -ne "Running") {
      Start-Service -Name "uvnc_service"
      L "uvnc_service started"
    } else {
      L "uvnc_service already running"
    }
  } else {
    L "uvnc_service not found"
  }
} catch { L ("uvnc_service error: " + `$_.Exception.Message) }

# Start websockify (kill old first)
try {
  Get-CimInstance Win32_Process | Where-Object { `$_.CommandLine -like "*websockify*" } | ForEach-Object {
    L ("Killing old websockify pid=" + `$_.ProcessId)
    Stop-Process -Id `$_.ProcessId -Force -ErrorAction SilentlyContinue
  }
} catch { L ("websockify cleanup error: " + `$_.Exception.Message) }

try {
  # Use cmd.exe so PATH resolution works the same as interactive
  `$cmd = 'python -m websockify 8080 localhost:5900 --web C:\CloudRAM\noVNC\noVNC-master'
  Start-Process -FilePath "cmd.exe" -ArgumentList "/c", `$cmd -WorkingDirectory "C:\CloudRAM" -WindowStyle Hidden
  L "websockify started"
} catch { L ("websockify start error: " + `$_.Exception.Message) }

# Start vm_server.py (kill old first)
try {
  Get-CimInstance Win32_Process | Where-Object { `$_.CommandLine -like "*C:\CloudRAM\vm_server.py*" } | ForEach-Object {
    L ("Killing old vm_server.py pid=" + `$_.ProcessId)
    Stop-Process -Id `$_.ProcessId -Force -ErrorAction SilentlyContinue
  }
} catch { L ("vm_server cleanup error: " + `$_.Exception.Message) }

try {
  `$cmd2 = 'python C:\CloudRAM\vm_server.py'
  Start-Process -FilePath "cmd.exe" -ArgumentList "/c", `$cmd2 -WorkingDirectory "C:\CloudRAM" -WindowStyle Hidden
  L "vm_server.py started"
} catch { L ("vm_server start error: " + `$_.Exception.Message) }

L "----- startup_services.ps1 end -----"
"@

        $servicesScript | Out-File -FilePath "C:\CloudRAM\startup_services.ps1" -Force -Encoding UTF8

        # Scheduled task (create or replace)
        $taskName = "CloudRAM-Services"
        $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-ExecutionPolicy Bypass -File C:\CloudRAM\startup_services.ps1"
        $trigger = New-ScheduledTaskTrigger -AtStartup
        $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest

        Register-ScheduledTask -Action $action -Trigger $trigger -Principal $principal -TaskName $taskName -Description "Start CloudRAM services every boot" -Force | Out-Null

        # Also run it right now (so resume works immediately)
        Start-Process -FilePath "powershell.exe" -ArgumentList "-ExecutionPolicy Bypass -File C:\CloudRAM\startup_services.ps1" -WindowStyle Hidden

        Write-EC2Log "✅ Ensured CloudRAM-Services task and ran startup_services.ps1"
    } catch {
        Write-EC2Log "ERROR Ensure-ServicesScriptsAndTask: $($_.Exception.Message)"
    }
}

# -----------------------------
# Main script
# -----------------------------
$currentState = Get-ScriptState
Write-EC2Log "Current state: $currentState"

switch ($currentState) {

    "INITIAL" {
        try {
            $adminPassword = "CloudRAM123!"
            net user Administrator $adminPassword | Out-Null
            net user Administrator /active:yes | Out-Null
        } catch {
            Write-EC2Log "ERROR: Failed to set Administrator password - $($_.Exception.Message)"
            exit 1
        }

        try {
            [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.ServicePointManager]::SecurityProtocol -bor 3072
            iex ((New-Object System.Net.WebClient).DownloadString('https://community.chocolatey.org/install.ps1'))
        } catch {
            Write-EC2Log "ERROR: Failed to install Chocolatey - $($_.Exception.Message)"
            exit 1
        }

        try {
            Start-Process -FilePath "choco" -ArgumentList "install python python-pip -y" -Wait -NoNewWindow -PassThru | Out-Null
            $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")
            Start-Process -FilePath "cmd.exe" -ArgumentList "/c pip install websockify flask flask-cors psutil boto3 watchdog" -Wait -NoNewWindow -PassThru | Out-Null
        } catch {
            Write-EC2Log "ERROR: Failed to install Python or packages - $($_.Exception.Message)"
            exit 1
        }

        try {
            Start-Process -FilePath "choco" -ArgumentList "install ultravnc -y" -Wait -NoNewWindow -PassThru | Out-Null
        } catch {
            Write-EC2Log "ERROR: Failed to install UltraVNC - $($_.Exception.Message)"
            exit 1
        }

        try {
            Start-Process -FilePath "choco" -ArgumentList "install googlechrome -y --ignore-checksums" -Wait -NoNewWindow -PassThru | Out-Null
        } catch {
            Write-EC2Log "ERROR: Failed to install Google Chrome - $($_.Exception.Message)"
            exit 1
        }

        try {
            Start-Process -FilePath "choco" -ArgumentList "install notepadplusplus -y --ignore-checksums" -Wait -NoNewWindow -PassThru | Out-Null
        } catch {
            Write-EC2Log "ERROR: Failed to install Notepad++ - $($_.Exception.Message)"
            exit 1
        }

        try {
            Start-Process -FilePath "choco" -ArgumentList "install vscode -y --ignore-checksums" -Wait -NoNewWindow -PassThru | Out-Null
        } catch {
            Write-EC2Log "ERROR: Failed to install VSCode - $($_.Exception.Message)"
            exit 1
        }

        try {
            $username = "Administrator"
            $password = "CloudRAM123!"
            Set-ItemProperty -Path "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon" -Name "AutoAdminLogon" -Value "1"
            Set-ItemProperty -Path "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon" -Name "DefaultUserName" -Value $username
            Set-ItemProperty -Path "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon" -Name "DefaultPassword" -Value $password
            Set-ItemProperty -Path "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon" -Name "ForceAutoLogon" -Value "1"
            Set-ItemProperty -Path "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System" -Name "DisableCAD" -Value 1
            Set-ItemProperty -Path "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System" -Name "PromptOnSecureDesktop" -Value 0
            Set-ItemProperty -Path "HKLM:\SOFTWARE\Policies\Microsoft\Windows\Personalization" -Name "NoLockScreen" -Value 1

            powercfg /change monitor-timeout-ac 0 | Out-Null
            powercfg /change monitor-timeout-dc 0 | Out-Null
            powercfg /change standby-timeout-ac 0 | Out-Null
            powercfg /change standby-timeout-dc 0 | Out-Null
            powercfg /change hibernate-timeout-ac 0 | Out-Null
            powercfg /change hibernate-timeout-dc 0 | Out-Null

            Write-EC2Log "Auto-login and power settings configured"
            Register-ResumeTask
            Set-ScriptState "POST_REBOOT_1"
            Restart-Computer -Force
        } catch {
            Write-EC2Log "ERROR: Failed to configure auto-login or power settings - $($_.Exception.Message)"
            exit 1
        }
    }

    "POST_REBOOT_1" {
        try {
            $ultravncIniPath = "C:\Program Files\uvnc bvba\UltraVNC\ultravnc.ini"
            if (-not (Test-Path $ultravncIniPath)) {
                $ultravncIniPath = "C:\Program Files\UltraVNC\ultravnc.ini"
            }

            $ultravncConfig = @"
[ultravnc]
passwd=
passwd2=
[admin]
UseRegistry=0
MSLogonRequired=0
NewMSLogon=0
DebugMode=0
Avilog=0
path=C:\Program Files\UltraVNC
accept_reject_mesg=
DebugLevel=0
DisableTrayIcon=0
rdpmode=0
LoopbackOnly=0
UseDSMPlugin=0
AllowLoopback=1
AuthRequired=0
ConnectPriority=0
DSMPlugin=
AuthHosts=
AllowShutdown=1
AllowProperties=1
AllowEditClients=1
[poll]
TurboMode=1
PollUnderCursor=0
PollFullScreen=1
OnlyPollConsole=0
OnlyPollOnEvent=0
MaxCpu=40
EnableDriver=0
EnableHook=1
EnableVirtual=0
SingleWindow=0
SingleApplicationName=
"@
            $ultravncConfig | Out-File -FilePath $ultravncIniPath -Encoding ASCII -Force

            $winvncPath = "C:\Program Files\uvnc bvba\UltraVNC\winvnc.exe"
            if (-not (Test-Path $winvncPath)) {
                $winvncPath = "C:\Program Files\UltraVNC\winvnc.exe"
            }

            Start-Process -FilePath $winvncPath -ArgumentList "-install" -NoNewWindow -Wait
            Start-Service -Name "uvnc_service" -ErrorAction Stop
        } catch {
            Write-EC2Log "ERROR: Failed to configure UltraVNC - $($_.Exception.Message)"
            exit 1
        }

        try {
            New-NetFirewallRule -DisplayName "Allow VNC (TCP 5900)" -Direction Inbound -Protocol TCP -LocalPort 5900 -Action Allow -Enabled True -ErrorAction SilentlyContinue | Out-Null
            New-NetFirewallRule -DisplayName "Allow noVNC (TCP 8080)" -Direction Inbound -Protocol TCP -LocalPort 8080 -Action Allow -Enabled True -ErrorAction SilentlyContinue | Out-Null
            New-NetFirewallRule -DisplayName "Allow Flask (TCP 5000)" -Direction Inbound -Protocol TCP -LocalPort 5000 -Action Allow -Enabled True -ErrorAction SilentlyContinue | Out-Null
            New-NetFirewallRule -DisplayName "Allow RDP (TCP 3389)" -Direction Inbound -Protocol TCP -LocalPort 3389 -Action Allow -Enabled True -ErrorAction SilentlyContinue | Out-Null
        } catch {
            Write-EC2Log "ERROR: Failed to configure firewall - $($_.Exception.Message)"
            exit 1
        }

        try {
            Invoke-WebRequest -Uri "https://github.com/novnc/noVNC/archive/refs/heads/master.zip" -OutFile "C:\CloudRAM\noVNC.zip" -UseBasicParsing
            Expand-Archive -Path "C:\CloudRAM\noVNC.zip" -DestinationPath "C:\CloudRAM\noVNC" -Force

            # IMPORTANT FIX: don't use host=127.0.0.1
            $indexPath = "C:\CloudRAM\noVNC\noVNC-master\index.html"
            $indexContent = @"
<!DOCTYPE html>
<html>
<head>
  <meta http-equiv="refresh" content="0; url=vnc.html?autoconnect=true&reconnect=true&reconnect_delay=5000">
</head>
<body>
  <p>Redirecting to VNC client...</p>
</body>
</html>
"@
            $indexContent | Set-Content -Path $indexPath -Force -Encoding UTF8
        } catch {
            Write-EC2Log "ERROR: Failed to setup noVNC - $($_.Exception.Message)"
            exit 1
        }

        try {
            $s3Url = "https://cloud-ram-scripts.s3.us-east-1.amazonaws.com/vm_server.py"
            Invoke-WebRequest -Uri $s3Url -OutFile "C:\CloudRAM\vm_server.py" -UseBasicParsing -ErrorAction Stop
        } catch {
            Write-EC2Log "ERROR: Failed to download vm_server.py from S3 - $($_.Exception.Message)"
            exit 1
        }

        # Create/ensure scheduled task + scripts, and run services now
        Ensure-ServicesScriptsAndTask

        Set-ScriptState "COMPLETED"
        Write-EC2Log "Setup completed successfully"

        Unregister-ScheduledTask -TaskName "CloudRAM-ResumeScript" -Confirm:$false -ErrorAction SilentlyContinue | Out-Null
    }

    "COMPLETED" {
        # ✅ THIS is the important fix: every boot, ensure services are up.
        Write-EC2Log "COMPLETED state - ensuring services are running after boot/stop-start"
        Ensure-ServicesScriptsAndTask
    }

    default {
        Set-ScriptState "INITIAL"
    }
}
</powershell>
