#requires -Version 5.1
<#
.SYNOPSIS
Build and restart this checkout's AgentScope backend and Web UI on Windows.
.DESCRIPTION
Uses the existing .venv and pnpm dependencies. Middleware is managed separately.
Builds the frontend and Node backend before stopping the existing services.
Runs the frontend build with Vite preview and the compiled Node backend.
Logs are kept in .cache\agentscope. Only this checkout's service trees are stopped.
.PARAMETER NoBrowser
Do not open the Web UI after startup.
.PARAMETER TimeoutSeconds
How long to wait for startup checks after launching the services.
#>
[CmdletBinding()]
param(
    [switch]$NoBrowser,
    [ValidateRange(10, 300)]
    [int]$TimeoutSeconds = 60
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$agentRoot = Split-Path -Parent $PSScriptRoot
$agentServiceDir = Join-Path $agentRoot 'examples\agent_service'
$agentUiDir = Join-Path $agentRoot 'examples\web_ui'
$agentPython = Join-Path $agentRoot '.venv\Scripts\python.exe'
$agentMain = Join-Path $agentServiceDir 'main.py'
$agentPorts = @(8000, 5173, 3000)
$agentUrl = 'http://localhost:5173/'
$agentLock = $null
$agentLogDir = $null

function Test-InProcessTree {
    param([int]$ProcessId, [hashtable]$Processes, [hashtable]$Ancestors)

    for ($depth = 0; $depth -lt 64 -and $ProcessId -gt 0; $depth++) {
        if ($Ancestors.ContainsKey($ProcessId)) { return $true }
        if (-not $Processes.ContainsKey($ProcessId)) { break }
        $child = $Processes[$ProcessId]
        $parentId = [int]$child.ParentProcessId
        if ($Processes.ContainsKey($parentId) -and
            $Processes[$parentId].CreationDate -gt $child.CreationDate) {
            break # The original parent exited and its PID was reused.
        }
        $ProcessId = $parentId
    }
    return $false
}

function Get-ServiceListeners {
    Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
        Where-Object { $_.LocalPort -in $agentPorts }
}

function Get-ProjectServiceRoots {
    $processMap = @{}
    $targetIds = @{}
    $allProcesses = @(Get-CimInstance Win32_Process)
    # Recognize both the old development processes and the compiled services.
    $backendPattern = '^"?' + [regex]::Escape($agentPython) +
        '"?\s+(?:-u\s+)?"?(?:' + [regex]::Escape($agentMain) +
        '|main\.py)"?(?:\s|$)'
    $uiPattern = '(?:vite[\\/]bin[\\/]vite\.js|' +
        'nodemon[\\/]bin[\\/]nodemon\.js|' +
        'ts-node[\\/]dist[\\/]bin\.js|' +
        'backend[\\/]dist[\\/]index\.js|' +
        'concurrently[\\/]dist[\\/]bin[\\/]concurrently\.js)'
    foreach ($process in $allProcesses) {
        $processMap[[int]$process.ProcessId] = $process
        $command = [string]$process.CommandLine
        $isBackend = $process.Name -eq 'python.exe' -and
            $command -match $backendPattern
        $isUi = $process.Name -eq 'node.exe' -and
            $command.IndexOf($agentUiDir + '\',
                [System.StringComparison]::OrdinalIgnoreCase) -ge 0 -and
            $command -match $uiPattern
        if ($isBackend -or $isUi) { $targetIds[[int]$process.ProcessId] = $true }
    }
    foreach ($listener in @(Get-ServiceListeners)) {
        if (-not (Test-InProcessTree -ProcessId $listener.OwningProcess `
            -Processes $processMap -Ancestors $targetIds)) {
            throw "Port $($listener.LocalPort) belongs to another program (PID $($listener.OwningProcess)). Close it before restarting."
        }
    }
    $allProcesses | Where-Object {
        $targetIds.ContainsKey([int]$_.ProcessId) -and
        -not (Test-InProcessTree -ProcessId $_.ParentProcessId `
            -Processes $processMap -Ancestors $targetIds)
    }
}

function Start-WebUiCommand {
    param(
        [ValidateSet('build', 'start')][string]$Command,
        [string]$Stdout,
        [string]$Stderr,
        [switch]$Wait
    )

    # Use the Windows shell, so this also works outside Codex. Encoding the
    # command preserves spaces/apostrophes in the pnpm installation path.
    $commandText = "& '{0}' {1}; exit " -f $pnpm.Source.Replace("'", "''"), $Command
    $commandText += '$LASTEXITCODE'
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($commandText))
    Start-Process -FilePath "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" `
        -ArgumentList @('-NoLogo', '-NoProfile', '-NonInteractive',
            '-ExecutionPolicy', 'Bypass', '-EncodedCommand', $encoded) `
        -WorkingDirectory $agentUiDir -WindowStyle Hidden `
        -RedirectStandardOutput $Stdout -RedirectStandardError $Stderr `
        -Wait:$Wait -PassThru
}

function Stop-ServiceTree {
    param($Process)

    $processId = [int]$Process.ProcessId
    $current = Get-CimInstance Win32_Process -Filter "ProcessId = $processId"
    if (-not $current) { return }
    if ($current.CreationDate -ne $Process.CreationDate -or
        $current.CommandLine -ne $Process.CommandLine) {
        throw "Process $processId changed identity; refusing to stop it."
    }
    Write-Host "Stopping $($Process.Name) (PID $processId)..."
    & "$env:SystemRoot\System32\taskkill.exe" /PID $processId /T /F | Out-Null
    if ($LASTEXITCODE -ne 0 -and
        (Get-Process -Id $processId -ErrorAction SilentlyContinue)) {
        throw "Could not stop PID $processId."
    }
}

function Test-ServiceResponse {
    param([string]$Url, [switch]$Json)

    try {
        # UseBasicParsing also works in Windows PowerShell 5.1 without IE setup.
        $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 3
        if ([int]$response.StatusCode -ne 200) { return $false }
        if ($Json) {
            if ($response.Headers['Content-Type'] -notlike 'application/json*') {
                return $false
            }
            $null = $response.Content | ConvertFrom-Json
        }
        return $true
    } catch {
        return $false
    }
}

try {
    Write-Host "AgentScope: $agentRoot"
    foreach ($required in @(
        $agentPython,
        $agentMain,
        (Join-Path $agentUiDir 'node_modules\.bin\concurrently.cmd'),
        (Join-Path $agentUiDir 'frontend\node_modules\.bin\vite.cmd'),
        (Join-Path $agentUiDir 'frontend\node_modules\.bin\tsc.cmd'),
        (Join-Path $agentUiDir 'backend\node_modules\.bin\tsc.cmd')
    )) {
        if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
            throw "Missing dependency: $required. Install project dependencies first."
        }
    }
    if (-not (Get-Command node.exe -ErrorAction SilentlyContinue)) {
        throw 'Node.js was not found on PATH.'
    }
    $pnpm = Get-Command pnpm.cmd -ErrorAction SilentlyContinue
    if (-not $pnpm) { throw 'pnpm.cmd was not found on PATH.' }

    $cacheDir = Join-Path $agentRoot '.cache\agentscope'
    $null = New-Item -ItemType Directory -Path $cacheDir -Force
    try {
        $agentLock = [System.IO.File]::Open(
            (Join-Path $cacheDir 'restart.lock'),
            [System.IO.FileMode]::OpenOrCreate,
            [System.IO.FileAccess]::ReadWrite,
            [System.IO.FileShare]::None
        )
    } catch {
        throw 'Another restart is in progress, or the restart lock is not writable.'
    }

    # Check ownership before building, then refresh the snapshot after the build.
    $null = @(Get-ProjectServiceRoots)

    $agentLogDir = Join-Path $cacheDir ((Get-Date -Format 'yyyyMMdd-HHmmss') + "-$PID")
    $null = New-Item -ItemType Directory -Path $agentLogDir
    $backendOut = Join-Path $agentLogDir 'backend.stdout.log'
    $backendErr = Join-Path $agentLogDir 'backend.stderr.log'
    $uiOut = Join-Path $agentLogDir 'webui.stdout.log'
    $uiErr = Join-Path $agentLogDir 'webui.stderr.log'
    $buildOut = Join-Path $agentLogDir 'build.stdout.log'
    $buildErr = Join-Path $agentLogDir 'build.stderr.log'

    Write-Host 'Building frontend and Node backend (pnpm build)...'
    Write-Host "Build logs: $agentLogDir"
    $build = Start-WebUiCommand -Command build -Stdout $buildOut -Stderr $buildErr -Wait
    if ($build.ExitCode -ne 0) {
        throw "Build failed (exit code $($build.ExitCode)). Existing service processes have not been stopped."
    }
    foreach ($artifact in @(
        (Join-Path $agentUiDir 'frontend\dist\index.html'),
        (Join-Path $agentUiDir 'backend\dist\index.js')
    )) {
        if (-not (Test-Path -LiteralPath $artifact -PathType Leaf)) {
            throw "Build output is missing: $artifact. Existing service processes have not been stopped."
        }
    }
    Write-Host 'Build completed.' -ForegroundColor Green

    $roots = @(Get-ProjectServiceRoots)
    foreach ($rootProcess in $roots) { Stop-ServiceTree $rootProcess }
    $stopDeadline = (Get-Date).AddSeconds(10)
    while (@(Get-ServiceListeners).Count -gt 0) {
        if ((Get-Date) -ge $stopDeadline) {
            throw 'Service ports are still occupied after stopping the old processes.'
        }
        Start-Sleep -Milliseconds 200
    }

    Write-Host 'Starting AgentScope backend and compiled Web UI...'
    $backend = Start-Process -FilePath $agentPython `
        -ArgumentList @('-u', ('"{0}"' -f $agentMain)) `
        -WorkingDirectory $agentServiceDir -WindowStyle Hidden `
        -RedirectStandardOutput $backendOut -RedirectStandardError $backendErr -PassThru

    $ui = Start-WebUiCommand -Command start -Stdout $uiOut -Stderr $uiErr

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $ready = $false
    do {
        $backend.Refresh()
        $ui.Refresh()
        if ($backend.HasExited -or $ui.HasExited) {
            throw 'A service exited before startup completed. See the logs below.'
        }
        $startupComplete = Select-String -LiteralPath $backendErr `
            -SimpleMatch 'Application startup complete.' -Quiet
        if ($startupComplete -and
            (Test-ServiceResponse $agentUrl) -and
            # The Python service listens on IPv4; avoid localhost's IPv6 fallback.
            (Test-ServiceResponse 'http://127.0.0.1:8000/branding' -Json) -and
            (Test-ServiceResponse 'http://127.0.0.1:3000/api/health' -Json) -and
            (Test-ServiceResponse 'http://localhost:5173/api/branding' -Json)) {
            $ready = $true
            break
        }
        Start-Sleep -Seconds 1
    } while ((Get-Date) -lt $deadline)

    if (-not $ready) {
        throw "Startup checks did not pass within $TimeoutSeconds seconds. Processes and logs are left available for inspection."
    }
    Write-Host "AgentScope restarted: $agentUrl" -ForegroundColor Green
    Write-Host "Logs: $agentLogDir"
    if (-not $NoBrowser) {
        try { Start-Process $agentUrl } catch {
            Write-Warning "Could not open the browser. Visit $agentUrl"
        }
    }
    exit 0
} catch {
    Write-Host "ERROR: $($_.Exception.Message)" -ForegroundColor Red
    if ($agentLogDir) {
        Write-Host "Logs: $agentLogDir"
        foreach ($logName in @('build.stdout.log', 'build.stderr.log',
            'backend.stderr.log', 'webui.stderr.log')) {
            $logPath = Join-Path $agentLogDir $logName
            if (Test-Path -LiteralPath $logPath) {
                Write-Host "Last lines of ${logName}:"
                Get-Content -LiteralPath $logPath -Tail 15
            }
        }
    }
    exit 1
} finally {
    if ($agentLock) { $agentLock.Dispose() }
}
