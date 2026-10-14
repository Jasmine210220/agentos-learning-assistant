$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Missing project Python environment.' }
$package = Get-Content -LiteralPath (Join-Path $projectRoot 'package.json') -Raw -Encoding utf8 | ConvertFrom-Json
function Get-SourceDigest([string]$filePath) {
    $stream = [System.IO.File]::OpenRead($filePath)
    $algorithm = [System.Security.Cryptography.SHA256]::Create()
    try { return ([System.BitConverter]::ToString($algorithm.ComputeHash($stream))).Replace('-','') }
    finally { $stream.Dispose(); $algorithm.Dispose() }
}
$records = @{}
foreach ($folder in @('backend','src','electron')) {
    Get-ChildItem -LiteralPath (Join-Path $projectRoot $folder) -Recurse -File |
        Where-Object { $_.Extension -in @('.py','.tsx','.ts','.css','.cjs') } |
        ForEach-Object { $records[$_.FullName.Substring($projectRoot.Length + 1)] = Get-SourceDigest $_.FullName }
}
foreach ($file in @('package.json','package-lock.json','agentos-backend.spec','requirements-lock.txt','scripts/build-release.ps1')) {
    $records[$file] = Get-SourceDigest (Join-Path $projectRoot $file)
}
$backendOutput = $package.build.extraResources[0].from
& $python -m PyInstaller --noconfirm --distpath $backendOutput --workpath ('test-output/build-release-' + $package.version) agentos-backend.spec
if ($LASTEXITCODE -ne 0) { throw 'Backend packaging failed.' }
& npm.cmd run build
if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed.' }
& (Join-Path $projectRoot 'node_modules\.bin\electron-builder.cmd') --win nsis
if ($LASTEXITCODE -ne 0) { throw 'Windows installer build failed.' }
foreach ($relativePath in $records.Keys) {
    if ((Get-SourceDigest (Join-Path $projectRoot $relativePath)) -ne $records[$relativePath]) { throw 'Source changed while packaging; rebuild before delivery.' }
}
$output = Join-Path $projectRoot $package.build.directories.output
@{version=$package.version;date=(Get-Date -Format o);source=$records;installer=(Get-SourceDigest (Join-Path $output ('AgentOS Lite Setup '+$package.version+'.exe')))} |
    ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $output 'source-manifest.json') -Encoding utf8
Write-Output 'Built current backend, frontend and Windows installer. Installation acceptance is still required.'
