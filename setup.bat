@echo off
setlocal EnableExtensions

if /I "%~1"=="--self-test" (
  echo INSTALLER_SELF_TEST=ok platform=windows-auto
  exit /b 0
)

set "ARCH=%PROCESSOR_ARCHITECTURE%"
if defined PROCESSOR_ARCHITEW6432 set "ARCH=%PROCESSOR_ARCHITEW6432%"
set "SCRIPT_NAME="
if /I "%ARCH%"=="AMD64" set "SCRIPT_NAME=setup-windows.bat"
if /I "%ARCH%"=="ARM64" set "SCRIPT_NAME=setup-windows-arm64.bat"
if not defined SCRIPT_NAME (
  echo ERROR: Unsupported Windows architecture: %ARCH%
  echo Plane Alerts supports Windows x64 and Windows ARM64.
  exit /b 2
)

if exist "%~dp0%SCRIPT_NAME%" (
  call "%~dp0%SCRIPT_NAME%" %*
  exit /b %ERRORLEVEL%
)

where powershell >nul 2>&1
if errorlevel 1 (
  echo ERROR: PowerShell is required to download the architecture-specific installer.
  exit /b 3
)

set "TMPDIR=%TEMP%\plane-alerts-setup-%RANDOM%"
mkdir "%TMPDIR%" >nul 2>&1
set "TARGET=%TMPDIR%\%SCRIPT_NAME%"
set "SUMS=%TMPDIR%\SHA256SUMS.txt"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$r=Invoke-RestMethod -UseBasicParsing 'https://api.github.com/repos/xtenrore/Plane-Alerts/releases/latest';" ^
  "if($r.draft -or $r.prerelease){throw 'Latest release is not stable'};" ^
  "$a=$r.assets ^| Where-Object {$_.name -eq '%SCRIPT_NAME%'} ^| Select-Object -First 1;" ^
  "$s=$r.assets ^| Where-Object {$_.name -eq 'SHA256SUMS.txt'} ^| Select-Object -First 1;" ^
  "if(-not $a -or -not $s){throw 'Required installer assets are missing'};" ^
  "Invoke-WebRequest -UseBasicParsing $a.browser_download_url -OutFile '%TARGET%';" ^
  "Invoke-WebRequest -UseBasicParsing $s.browser_download_url -OutFile '%SUMS%'"
if errorlevel 1 (
  echo ERROR: Could not download verified installer assets from the latest stable GitHub release.
  rmdir /s /q "%TMPDIR%" >nul 2>&1
  exit /b 4
)

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$line=Get-Content '%SUMS%' ^| Where-Object {$_ -match '\s%SCRIPT_NAME%$'} ^| Select-Object -First 1;" ^
  "if(-not $line){exit 5};" ^
  "$expected=($line -split '\s+')[0].ToLower();" ^
  "$actual=(Get-FileHash -Algorithm SHA256 '%TARGET%').Hash.ToLower();" ^
  "if($actual -ne $expected){exit 6}"
if errorlevel 1 (
  echo ERROR: Installer checksum verification failed. Nothing was executed.
  rmdir /s /q "%TMPDIR%" >nul 2>&1
  exit /b 5
)

call "%TARGET%" %*
set "RC=%ERRORLEVEL%"
rmdir /s /q "%TMPDIR%" >nul 2>&1
exit /b %RC%
