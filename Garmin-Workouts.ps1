$ErrorActionPreference = 'Stop'
$pythonPath = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    $pythonPath = (Get-Command python -ErrorAction Stop).Source
}
if ($args.Count -gt 0 -and $args[0] -eq 'setup') {
    & $pythonPath -m pip install --target (Join-Path $PSScriptRoot '.workout-packages') -r (Join-Path $PSScriptRoot 'requirements-workouts.txt') --disable-pip-version-check
} else {
    & $pythonPath (Join-Path $PSScriptRoot 'garmin_workouts.py') @args
}
exit $LASTEXITCODE
