param(
    [ValidateSet('release', 'candidate')]
    [string]$Profile = 'release'
)

$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$outputDir = Join-Path $projectRoot $Profile
$workDir = Join-Path $projectRoot ('build-' + $Profile)
$auditDir = Join-Path $projectRoot 'audit'
$buildName = if ($Profile -eq 'release') { '小米遥控器' } else { '小米遥控器-语音恢复候选版' }
$previousBuildName = $env:MIREMOTE_BUILD_NAME
$previousProfile = $env:MIREMOTE_BUILD_PROFILE
Push-Location -LiteralPath $projectRoot
try {
    New-Item -ItemType Directory -Path $outputDir,$auditDir -Force | Out-Null
    $env:MIREMOTE_BUILD_NAME = $buildName
    $env:MIREMOTE_BUILD_PROFILE = $Profile
    & python -m PyInstaller --noconfirm --distpath $outputDir --workpath $workDir miremote.spec
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed: $LASTEXITCODE" }
    $exe = Join-Path $outputDir ($buildName + '.exe')
    $reportPath = Join-Path $auditDir ($Profile + '-runtime-check.json')
    $checkProcess = Start-Process -FilePath $exe -ArgumentList @('--runtime-check', ('"{0}"' -f $reportPath)) -WindowStyle Hidden -PassThru
    if (-not $checkProcess.WaitForExit(60000)) { throw 'Runtime check exceeded 60 seconds.' }
    if ($checkProcess.ExitCode -ne 0) { throw "Runtime check failed: $($checkProcess.ExitCode)" }
    $report = Get-Content -LiteralPath $reportPath -Raw -Encoding utf8 | ConvertFrom-Json
    if (-not $report.ok -or -not $report.frozen -or $report.hardware_started -or $report.profile -ne $Profile) {
        throw 'Packaged identity or dependency validation failed.'
    }
    Write-Output "Verified build: $exe"
} finally {
    $env:MIREMOTE_BUILD_NAME = $previousBuildName
    $env:MIREMOTE_BUILD_PROFILE = $previousProfile
    Pop-Location
}
