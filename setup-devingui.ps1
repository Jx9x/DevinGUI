param([string]$Python)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$candidates = @()
if ($Python) {
    $candidates += @{ Program = $Python; Arguments = @() }
} else {
    $existing = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $existing -PathType Leaf) {
        $candidates += @{ Program = $existing; Arguments = @() }
    } else {
        $candidates += @{ Program = 'py'; Arguments = @('-3') }
        $candidates += @{ Program = 'python3'; Arguments = @() }
        $candidates += @{ Program = 'python'; Arguments = @() }
    }
}

$interpreter = $null
foreach ($candidate in $candidates) {
    try {
        $program = $candidate.Program
        $arguments = $candidate.Arguments
        $resolved = & $program @arguments -c 'import sys; print(sys.executable); sys.exit(0 if sys.version_info >= (3, 12) else 1)' 2>$null
        if ($LASTEXITCODE -eq 0 -and $resolved -and (Test-Path -LiteralPath $resolved -PathType Leaf)) {
            $interpreter = $resolved
            break
        }
    } catch {
        continue
    }
}

if (-not $interpreter) {
    Write-Host '[setup] No usable Python 3.12+ interpreter was found.'
    Write-Host '[setup] Install Python 3.12+ or pass -Python with its absolute executable path.'
    Write-Host '[setup] An existing incompatible .venv is never deleted or replaced automatically.'
    exit 1
}

& $interpreter (Join-Path $PSScriptRoot 'scripts\setup_devingui.py')
exit $LASTEXITCODE
