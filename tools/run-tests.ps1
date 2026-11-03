param([Parameter(ValueFromRemainingArguments = $true)][string[]] $TestArguments)
$ErrorActionPreference = 'Stop'
$kitRoot = [System.IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$scratchRoot = Join-Path $kitRoot '.test-tmp'
$runDirectory = Join-Path $scratchRoot ([guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $runDirectory -Force | Out-Null
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:TEMP = $runDirectory
$env:TMP = $runDirectory
$env:TMPDIR = $runDirectory
Push-Location -LiteralPath $kitRoot
try {
    if ($TestArguments.Count -eq 0) { $TestArguments = @('check') }
    & python -X utf8 -m unittest @TestArguments
    $exitCode = $LASTEXITCODE
}
finally {
    Pop-Location
    try { [System.IO.Directory]::Delete($runDirectory, $false) } catch { }
    try { [System.IO.Directory]::Delete($scratchRoot, $false) } catch { }
}
exit $exitCode
