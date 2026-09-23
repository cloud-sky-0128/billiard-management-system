param([string]$Python = "python")

$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$BuildDirectory = Join-Path $ProjectRoot "build\window-preview"
$DistDirectory = Join-Path $ProjectRoot "dist\window-preview"
$OutputDirectory = Join-Path $DistDirectory "BilliardManagerWindow"
$ZipPath = Join-Path $ProjectRoot "dist\BilliardManagerWindow-preview.zip"
$GuidePath = Join-Path $ProjectRoot "docs\WINDOWS_WINDOW_PREVIEW.md"

Set-Location $ProjectRoot

foreach ($Path in @($BuildDirectory, $DistDirectory, $ZipPath)) {
    if (Test-Path -LiteralPath $Path) {
        $ResolvedPath = (Resolve-Path -LiteralPath $Path).Path
        if (-not $ResolvedPath.StartsWith($ProjectRoot + [IO.Path]::DirectorySeparatorChar)) {
            throw "Refusing to remove path outside project: $ResolvedPath"
        }
        Remove-Item -LiteralPath $ResolvedPath -Recurse -Force
    }
}

& $Python -m PyInstaller --noconfirm --clean --workpath $BuildDirectory --distpath $DistDirectory billiard-manager-window.spec
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller window build failed. Install requirements-window.txt first."
}

Copy-Item -LiteralPath $GuidePath -Destination (Join-Path $OutputDirectory "README.txt")
Compress-Archive -Path $OutputDirectory -DestinationPath $ZipPath -CompressionLevel Optimal

Write-Host "Windows window preview package created: $ZipPath"
