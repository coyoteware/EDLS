<#
EDLS release build: the app as a PyInstaller one-folder bundle, wrapped in an
Inno Setup installer.

    .\build.ps1
    (if PowerShell says scripts are disabled:
     powershell -ExecutionPolicy Bypass -File .\build.ps1)

Needs Python 3.11+ on PATH and Inno Setup 6. The Python side runs in its own
build\venv (requirements.txt + PyInstaller), never your global Python, so the
bundle holds only what EDLS actually uses.

Output (build\ and dist\ are git-ignored):
    dist\EDLS\EDLS.exe              the app
    dist\EDLS-Setup-<version>.exe   the installer; its SHA256 is printed last

The version comes from config.APP_VERSION, the single source of truth.
#>

$root = $PSScriptRoot
$build = Join-Path $root "build"
$dist = Join-Path $root "dist"

function Fail($message) {
    Write-Host "BUILD FAILED: $message" -ForegroundColor Red
    exit 1
}

# 1. The build venv, kept between builds.
$venv = Join-Path $build "venv"
$py = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Host "Creating build venv..."
    python -m venv $venv
    if ($LASTEXITCODE -ne 0) { Fail "couldn't create $venv (is Python 3.11+ on PATH?)" }
}
& $py -m pip install --disable-pip-version-check --quiet -r (Join-Path $root "requirements.txt") pyinstaller
if ($LASTEXITCODE -ne 0) { Fail "pip install failed" }

# 2. The version. FileVersion is the four-number form Windows wants.
Push-Location $root
$version = & $py -c "import config; print(config.APP_VERSION)"
$code = $LASTEXITCODE
Pop-Location
if ($code -ne 0 -or -not $version) { Fail "couldn't read config.APP_VERSION" }
$version = "$version".Trim()
$nums = @($version.Split(".") | ForEach-Object { [int]($_ -replace "\D.*$", "") })
while ($nums.Count -lt 4) { $nums += 0 }
$fileVersion = $nums[0..3] -join "."
Write-Host "Building EDLS $version"

# 3. The exe's version resource (Properties > Details), in PyInstaller's format.
$copyright = (Select-String -Path (Join-Path $root "LICENSE") -Pattern "^Copyright" |
              Select-Object -First 1).Line.Trim()
$tuple = $nums[0..3] -join ", "
$versionInfo = @"
VSVersionInfo(
  ffi=FixedFileInfo(filevers=($tuple), prodvers=($tuple), mask=0x3f, flags=0x0,
                    OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'CMDR Coyote Bongwater'),
      StringStruct('FileDescription', 'Elite: Dangerous Launch Suite'),
      StringStruct('FileVersion', '$version'),
      StringStruct('InternalName', 'EDLS'),
      StringStruct('LegalCopyright', '$copyright'),
      StringStruct('OriginalFilename', 'EDLS.exe'),
      StringStruct('ProductName', 'EDLS'),
      StringStruct('ProductVersion', '$version')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"@
New-Item -ItemType Directory -Force $build | Out-Null
$versionFile = Join-Path $build "version_info.txt"
[System.IO.File]::WriteAllText($versionFile, $versionInfo,
                               (New-Object System.Text.UTF8Encoding $false))

# 4. The app. One-folder, not one-file: nothing unpacks to %TEMP% on every
#    start, antivirus flags it less, and Qt's DLLs stay replaceable files.
$assets = Join-Path $root "assets"
& $py -m PyInstaller --noconfirm --clean --windowed --noupx `
    --name EDLS `
    --icon (Join-Path $assets "icon.ico") `
    --add-data "$assets;assets" `
    --version-file $versionFile `
    --specpath $build `
    --workpath (Join-Path $build "pyinstaller") `
    --distpath $dist `
    (Join-Path $root "main.py")
if ($LASTEXITCODE -ne 0) { Fail "PyInstaller failed" }

# 5. The installer.
$iscc = @(
    (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"),
    (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe"),
    (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe")
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { Fail "Inno Setup 6 not found - get it from https://jrsoftware.org/isinfo.php" }
& $iscc /Q "/DAppVersion=$version" "/DFileVersion=$fileVersion" (Join-Path $root "installer.iss")
if ($LASTEXITCODE -ne 0) { Fail "Inno Setup failed" }

$setup = Join-Path $dist "EDLS-Setup-$version.exe"
Write-Host ""
Write-Host "App:        $(Join-Path $dist 'EDLS\EDLS.exe')"
Write-Host "Installer:  $setup"
Write-Host "SHA256:     $((Get-FileHash -Algorithm SHA256 $setup).Hash)"
