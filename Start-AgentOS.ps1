$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$pythonPath = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (Test-Path -LiteralPath $pythonPath) { $env:AGENTOS_PYTHON = $pythonPath }
$electronPath = Join-Path $PSScriptRoot 'node_modules\electron\dist\electron.exe'
if (-not (Test-Path -LiteralPath $electronPath)) { throw 'Please run npm install first.' }
if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'dist\index.html'))) { throw 'Please run npm run build first.' }
Start-Process -FilePath $electronPath -ArgumentList ('"' + $PSScriptRoot + '"') -WorkingDirectory $PSScriptRoot -WindowStyle Hidden
