$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$BuildDirectory = Join-Path $ProjectRoot "build"
$OutputDirectory = Join-Path $ProjectRoot "dist\BilliardManager"
$ZipPath = Join-Path $ProjectRoot "dist\BilliardManager-windows-x64.zip"
$GuidePath = Join-Path $ProjectRoot "docs\WINDOWS_PORTABLE.md"

Set-Location $ProjectRoot

foreach ($Path in @($BuildDirectory, $OutputDirectory, $ZipPath)) {
    if (Test-Path -LiteralPath $Path) {
        $ResolvedPath = (Resolve-Path -LiteralPath $Path).Path
        if (-not $ResolvedPath.StartsWith($ProjectRoot + [IO.Path]::DirectorySeparatorChar)) {
            throw "Refusing to remove path outside project: $ResolvedPath"
        }
        Remove-Item -LiteralPath $ResolvedPath -Recurse -Force
    }
}

python -m PyInstaller --noconfirm --clean billiard-manager.spec
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller build failed."
}

Copy-Item -LiteralPath $GuidePath -Destination (Join-Path $OutputDirectory "README.txt")
Compress-Archive -Path $OutputDirectory -DestinationPath $ZipPath -CompressionLevel Optimal

Write-Host "Windows portable package created: $ZipPath"
