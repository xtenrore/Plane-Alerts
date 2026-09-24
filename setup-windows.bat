@echo off
setlocal EnableExtensions EnableDelayedExpansion

set "TARGET_ARCH=AMD64"
set "TARGET_LABEL=Windows x64 / AMD64"
set "INSTALL_DIR=%ProgramData%\PlaneAlerts"
set "REPO_URL=https://github.com/xtenrore/Plane-Alerts.git"
set "API_URL=https://api.github.com/repos/xtenrore/Plane-Alerts/releases/latest"

if /I "%~1"=="--self-test" (
  echo INSTALLER_SELF_TEST=ok platform=windows-x64
  exit /b 0
)

set "ARCH=%PROCESSOR_ARCHITECTURE%"
if defined PROCESSOR_ARCHITEW6432 set "ARCH=%PROCESSOR_ARCHITEW6432%"
if /I not "%ARCH%"=="%TARGET_ARCH%" (
  echo ERROR: This installer is for %TARGET_LABEL%. Detected architecture: %ARCH%
  echo Download setup-windows-arm64.bat for Windows ARM64.
  exit /b 2
)
if /I "%~1"=="--check-platform" (
  echo PLATFORM_CHECK=ok platform=windows-x64 arch=%ARCH%
  exit /b 0
)

net session >nul 2>&1
if errorlevel 1 (
  echo ERROR: Plane Alerts setup needs Administrator permission to install background tasks.
  echo Right-click this file and choose "Run as administrator", then try again.
  exit /b 5
)

echo.
echo Plane Alerts Setup - %TARGET_LABEL%
echo ==============================================
echo Installation directory: %INSTALL_DIR%
echo.

where winget >nul 2>&1
if errorlevel 1 (
  echo ERROR: Windows Package Manager ^(winget^) is unavailable.
  echo Install or update "App Installer" from Microsoft, then run this installer again.
  exit /b 6
)

where git >nul 2>&1
if errorlevel 1 (
  echo Installing Git from the official winget package...
  winget install --id Git.Git -e --source winget --accept-source-agreements --accept-package-agreements
  if errorlevel 1 exit /b 7
  set "PATH=%ProgramFiles%\Git\cmd;%PATH%"
)
git --version || exit /b 7

where py >nul 2>&1
if errorlevel 1 (
  echo Installing Python 3.11 from the official winget package...
  winget install --id Python.Python.3.11 -e --source winget --accept-source-agreements --accept-package-agreements
  if errorlevel 1 exit /b 8
)
py -3.11 --version >nul 2>&1
if errorlevel 1 (
  echo Installing Python 3.11 from the official winget package...
  winget install --id Python.Python.3.11 -e --source winget --accept-source-agreements --accept-package-agreements
  if errorlevel 1 exit /b 8
)
py -3.11 --version || exit /b 8

if exist "%INSTALL_DIR%\.git" goto existing
if exist "%INSTALL_DIR%" (
  echo ERROR: %INSTALL_DIR% exists but is not a Plane Alerts Git installation.
  echo Move or rename that directory, then run setup again.
  exit /b 9
)

for /f "usebackq delims=" %%T in (`powershell -NoProfile -Command "$r=Invoke-RestMethod -UseBasicParsing '%API_URL%'; if($r.draft -or $r.prerelease){exit 3}; $r.tag_name"`) do set "RELEASE_TAG=%%T"
if not defined RELEASE_TAG (
  echo ERROR: Could not determine the latest stable Plane Alerts release from GitHub.
  exit /b 10
)
echo Installing stable release %RELEASE_TAG%...
git clone --branch "%RELEASE_TAG%" --single-branch --depth 1 "%REPO_URL%" "%INSTALL_DIR%"
if errorlevel 1 exit /b 11

goto launch

:existing
echo Existing Plane Alerts installation detected. Opening setup/repair menu...

:launch
pushd "%INSTALL_DIR%"
for /f "delims=" %%R in ('git remote get-url origin') do set "ORIGIN=%%R"
echo !ORIGIN! | findstr /I /C:"github.com/xtenrore/Plane-Alerts" >nul
if errorlevel 1 (
  popd
  echo ERROR: Existing Git repository is not the official Plane Alerts repository.
  exit /b 12
)
if not exist ".venv\Scripts\python.exe" (
  py -3.11 -m venv .venv
  if errorlevel 1 (popd & exit /b 13)
)
".venv\Scripts\python.exe" "scripts\community_installer_v55.py" --install-dir "%INSTALL_DIR%"
set "RC=%ERRORLEVEL%"
popd
exit /b %RC%
