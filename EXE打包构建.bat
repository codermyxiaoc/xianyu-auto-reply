@echo off
setlocal DisableDelayedExpansion
REM === XianyuAutoReply EXE Build Script (One-Click) ===
chcp 65001 >nul
cd /d "%~dp0"
set "BUILD_EXIT_CODE=1"
set "BUILD_PYTHON=python"
if exist "%~dp0.venv\Scripts\python.exe" set "BUILD_PYTHON=%~dp0.venv\Scripts\python.exe"

echo ============================================
echo   XianyuAutoReply - One-Click Build
echo ============================================
echo.

REM --- Validate desktop URL before cleanup, dependency installation or compilation. ---
REM URL input is read by Python (stdin or XIANYU_DESKTOP_BASE_URL), never expanded by cmd.
"%BUILD_PYTHON%" --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found.
    goto :end
)
:desktop_temp_path
set "DESKTOP_CONFIG_ID=%RANDOM%-%RANDOM%-%RANDOM%"
set "DESKTOP_CONFIG_TMP=%TEMP%\xianyu-desktop-%DESKTOP_CONFIG_ID%.json"
set "DESKTOP_MODE_TMP=%TEMP%\xianyu-desktop-%DESKTOP_CONFIG_ID%.mode"
if exist "%DESKTOP_CONFIG_TMP%" goto :desktop_temp_path
if exist "%DESKTOP_MODE_TMP%" goto :desktop_temp_path
"%BUILD_PYTHON%" -B "%~dp0scripts\configure_desktop.py" --interactive --output "%DESKTOP_CONFIG_TMP%" --print-mode > "%DESKTOP_MODE_TMP%"
if errorlevel 1 (
    echo [ERROR] Invalid desktop configuration. Nothing was cleaned or compiled.
    goto :end
)
set /p "DESKTOP_MODE=" < "%DESKTOP_MODE_TMP%"
if "%DESKTOP_MODE%"=="local" goto :desktop_config_ready
if "%DESKTOP_MODE%"=="remote" goto :desktop_config_ready
echo [ERROR] Desktop mode was not generated.
goto :end

:desktop_config_ready
echo [INFO] Desktop mode: %DESKTOP_MODE%

REM --- Ask whether to use local proxy ---
choice /C YN /M "Use local proxy for downloads? (Y=yes, N=no)"
if %errorlevel% equ 1 (
    echo [INFO] Setting Clash local proxy: http://127.0.0.1:7890
    set HTTP_PROXY=http://127.0.0.1:7890
    set HTTPS_PROXY=http://127.0.0.1:7890
    echo [INFO] Proxy configured successfully.
) else (
    echo [INFO] No proxy configured.
)
echo.

REM --- Ask whether to skip frontend build ---
set "SKIP_FRONTEND=0"
if "%DESKTOP_MODE%"=="remote" goto :after_frontend_questions
choice /C YN /M "Skip frontend build (Y=skip, N=rebuild)"
if %errorlevel% equ 1 set SKIP_FRONTEND=1
echo.

REM --- Check node/npm ---
call npm --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] npm not found, please install Node.js first.
    goto :end
)

:after_frontend_questions
REM --- Check Nuitka ---
"%BUILD_PYTHON%" -c "import nuitka" 2>nul
if errorlevel 1 (
    echo [INFO] Installing Nuitka...
    "%BUILD_PYTHON%" -m pip install nuitka ordered-set zstandard
    if errorlevel 1 (
        echo [ERROR] Failed to install Nuitka.
        goto :end
    )
)

REM --- Read version from launcher/version.py ---
"%BUILD_PYTHON%" -B -c "from launcher.version import CURRENT_VERSION; print(CURRENT_VERSION)" > "%DESKTOP_MODE_TMP%"
if errorlevel 1 goto :end
set /p "APP_VER=" < "%DESKTOP_MODE_TMP%"
echo [INFO] Current version: v%APP_VER%
echo.

echo [1/6] Cleaning old build output...
"%BUILD_PYTHON%" -B "%~dp0scripts\prepare_desktop_build.py" clean
if errorlevel 1 (
    echo [ERROR] Build directory validation or cleanup failed.
    goto :end
)
echo        Done.

if "%DESKTOP_MODE%"=="remote" goto :remote_frontend
if %SKIP_FRONTEND% equ 1 goto :skip_frontend

echo [2/6] Building frontend...
cd frontend
call npm ci --no-audit --no-fund
if errorlevel 1 (
    echo [ERROR] npm ci failed.
    cd ..
    goto :end
)
call npm run build
if errorlevel 1 (
    echo [ERROR] npm run build failed.
    cd ..
    goto :end
)
cd ..

if not exist "frontend\dist\index.html" (
    echo [ERROR] Frontend build output not found.
    goto :end
)
echo [INFO] Frontend build complete.
goto :after_frontend

:skip_frontend
echo [2/6] Skipping frontend build.
if not exist "frontend\dist\index.html" (
    echo [ERROR] frontend\dist not found. Run full build first.
    goto :end
)
goto :after_frontend

:remote_frontend
echo [2/6] Remote mode: using server pages, no local frontend build required.

:after_frontend

echo [3/6] Nuitka compiling launcher (may take several minutes)...
if "%DESKTOP_MODE%"=="remote" goto :compile_remote

REM --- Full local package, including all local service dependencies. ---
"%BUILD_PYTHON%" -m nuitka ^
    --standalone ^
    --windows-console-mode=disable ^
    --windows-company-name=XianyuAutoReply ^
    --windows-product-name=XianyuAutoReply ^
    --windows-product-version=%APP_VER%.0 ^
    --windows-file-version=%APP_VER%.0 ^
    --output-filename=XianyuAutoReply.exe ^
    --output-dir=build_output ^
    --include-package=launcher ^
    --include-package=common ^
    --include-package=http ^
    --include-package=urllib ^
    --include-package=email ^
    --include-package=playwright ^
    --include-package-data=playwright ^
    --include-package=patchright ^
    --include-package-data=patchright ^
    --include-package=uvicorn ^
    --include-package=fastapi ^
    --include-package=sqlalchemy ^
    --include-package=pydantic ^
    --include-package=pydantic_settings ^
    --include-package=email_validator ^
    --include-package=asyncmy ^
    --include-package=pymysql ^
    --include-package=redis ^
    --include-package=aiohttp ^
    --include-package=aiohttp_socks ^
    --include-package=httpx ^
    --include-package=httpcore ^
    --include-package=loguru ^
    --include-package=passlib ^
    --include-package=jose ^
    --include-package=Crypto ^
    --include-package=websockets ^
    --include-package=multipart ^
    --include-package=PIL ^
    --include-package=anyio ^
    --include-package=starlette ^
    --include-package=click ^
    --include-package=h11 ^
    --include-package=sniffio ^
    --include-package=idna ^
    --include-package=certifi ^
    --include-package=dateutil ^
    --include-package=apscheduler ^
    --include-package=python_socks ^
    --include-package=requests ^
    --include-package=pandas ^
    --include-package=openpyxl ^
    --include-package=qrcode ^
    --include-package=openai ^
    --include-package=bcrypt ^
    --nofollow-import-to=test ^
    --nofollow-import-to=tests ^
    --nofollow-import-to=launcher.test_* ^
    --nofollow-import-to=docs ^
    --nofollow-import-to=setuptools ^
    --nofollow-import-to=pip ^
    --nofollow-import-to=wheel ^
    --nofollow-import-to=nuitka ^
    --nofollow-import-to=pandas.tests ^
    --nofollow-import-to=sqlalchemy.testing ^
    --nofollow-import-to=passlib.tests ^
    --nofollow-import-to=Crypto.SelfTest ^
    --nofollow-import-to=qrcode.tests ^
    --nofollow-import-to=PyInstaller ^
    --nofollow-import-to=pygments ^
    --nofollow-import-to=pydoc ^
    --nofollow-import-to=doctest ^
    --nofollow-import-to=unittest ^
    --enable-plugin=tk-inter ^
    --assume-yes-for-downloads launcher\main.py
goto :after_compile

:compile_remote
REM --- Server client: only the launcher, standard library and Tk runtime. ---
"%BUILD_PYTHON%" -m nuitka ^
    --standalone ^
    --msvc=latest ^
    --windows-console-mode=disable ^
    --windows-company-name=XianyuAutoReply ^
    --windows-product-name=XianyuAutoReply ^
    --windows-product-version=%APP_VER%.0 ^
    --windows-file-version=%APP_VER%.0 ^
    --output-filename=XianyuAutoReply.exe ^
    --output-dir=build_output ^
    --report=build_output\nuitka-remote-report.xml ^
    --include-module=launcher._bootstrap ^
    --include-module=launcher.desktop_config ^
    --include-module=launcher.frozen_detect ^
    --include-module=launcher.remote_gui ^
    --include-module=launcher.gui_theme ^
    --nofollow-import-to=_bootstrap ^
    --nofollow-import-to=launcher.gui ^
    --nofollow-import-to=launcher.config_init ^
    --nofollow-import-to=launcher.browser_setup ^
    --nofollow-import-to=launcher.service_runner ^
    --nofollow-import-to=common ^
    --nofollow-import-to=test ^
    --nofollow-import-to=tests ^
    --nofollow-import-to=launcher.test_* ^
    --nofollow-import-to=unittest ^
    --nofollow-import-to=setuptools ^
    --nofollow-import-to=pip ^
    --nofollow-import-to=nuitka ^
    --enable-plugin=tk-inter ^
    --assume-yes-for-downloads launcher\main.py

:after_compile
if errorlevel 1 (
    echo.
    echo [ERROR] Nuitka compile failed, check errors above.
    goto :end
)

echo [4/6] Copying project files to dist...
set DIST_DIR=build_output\main.dist
if "%DESKTOP_MODE%"=="remote" goto :after_browsers

REM --- Copy sub-service source and common ---
xcopy /E /I /Y "backend-web" "%DIST_DIR%\backend-web" >nul
xcopy /E /I /Y "websocket" "%DIST_DIR%\websocket" >nul
xcopy /E /I /Y "scheduler" "%DIST_DIR%\scheduler" >nul
xcopy /E /I /Y "common" "%DIST_DIR%\common" >nul
if exist "frontend\dist\index.html" xcopy /E /I /Y "frontend\dist" "%DIST_DIR%\frontend\dist" >nul

REM --- Local sub-services need a complete standalone Python runtime and this venv's packages. ---
"%BUILD_PYTHON%" -B "%~dp0scripts\bundle_desktop_runtime.py"
if errorlevel 1 (
    echo [ERROR] Failed to bundle the local Python runtime.
    goto :end
)
set "PACKAGED_BROWSER_DIR=%DIST_DIR%\ms-playwright"
mkdir "%PACKAGED_BROWSER_DIR%" 2>nul
set "PLAYWRIGHT_BROWSERS_PATH=%PACKAGED_BROWSER_DIR%"
echo [INFO] Installing Chromium into package dir...
"%BUILD_PYTHON%" -m playwright install chromium
if errorlevel 1 (
    echo [ERROR] Failed to bundle Chromium browser.
    goto :end
)
"%BUILD_PYTHON%" -m patchright install chromium
if errorlevel 1 (
    echo [ERROR] Failed to bundle Patchright Chromium browser.
    goto :end
)

:after_browsers
REM --- Clean temp files from copies ---
"%BUILD_PYTHON%" -B "%~dp0scripts\prepare_desktop_build.py" sanitize-dist
if errorlevel 1 (
    echo [ERROR] Refused to clean unsafe package paths.
    goto :end
)
copy /Y "%DESKTOP_CONFIG_TMP%" "%DIST_DIR%\desktop-config.json" >nul
if errorlevel 1 (
    echo [ERROR] Failed to include desktop-config.json.
    goto :end
)

echo [5/6] Moving to release dir...
"%BUILD_PYTHON%" "%~dp0scripts\copy_with_progress.py" "%DIST_DIR%" "release\XianyuAutoReply"
if errorlevel 1 (
    echo [INFO] Fallback to xcopy...
    xcopy /E /I /Y "%DIST_DIR%" "release\XianyuAutoReply" >nul
)
if not exist "release\XianyuAutoReply\logs" mkdir "release\XianyuAutoReply\logs" >nul
REM --- Copy compiled EXE into release dir to match the dist contents ---
if exist "build_output\XianyuAutoReply.exe" (
    copy /Y "build_output\XianyuAutoReply.exe" "release\XianyuAutoReply\XianyuAutoReply.exe" >nul
    echo [INFO] Copied EXE to release dir
) else (
    echo [WARN] Compiled EXE not found at build_output\XianyuAutoReply.exe
)

REM --- Remove data dir from release (user data should not be packaged) ---
"%BUILD_PYTHON%" -B "%~dp0scripts\prepare_desktop_build.py" sanitize-release
if errorlevel 1 (
    echo [ERROR] Refused to clean unsafe release paths.
    goto :end
)
if not exist "release\XianyuAutoReply\desktop-config.json" (
    echo [ERROR] Release desktop-config.json is missing.
    goto :end
)

echo [6/6] Packaging release zip...
set ZIP_NAME=app-v%APP_VER%.zip
if "%DESKTOP_MODE%"=="remote" set "ZIP_NAME=XianyuAutoReply-v%APP_VER%-server-win64.zip"
if exist "release\%ZIP_NAME%" del /f "release\%ZIP_NAME%"

echo        Source: %CD%\release\XianyuAutoReply
echo        Target: %CD%\release\%ZIP_NAME%

REM --- Compress: prefer 7-Zip (fast, multi-threaded), fallback to Python zipfile (stored mode) ---
set ZIP_OK=0
where 7z >nul 2>&1
if %errorlevel% equ 0 (
    echo [INFO] Using 7-Zip for fast compression...
    7z a -tzip -mx=1 -mmt=on "release\%ZIP_NAME%" ".\release\XianyuAutoReply\*"
    if not errorlevel 1 set ZIP_OK=1
)

if %ZIP_OK% equ 0 (
    echo [INFO] Using Python zipfile...
    "%BUILD_PYTHON%" "%~dp0scripts\zip_with_progress.py" "%CD%\release\XianyuAutoReply" "%CD%\release\%ZIP_NAME%"
)

if not exist "release\%ZIP_NAME%" (
    echo [INFO] Fallback to PowerShell Compress-Archive...
    powershell -Command "Compress-Archive -Path 'release\XianyuAutoReply\*' -DestinationPath 'release\%ZIP_NAME%' -Force"
)

if not exist "release\%ZIP_NAME%" (
    echo [ERROR] Failed to create zip package.
    goto :end
)

echo.
echo ============================================
echo   Build Complete!
echo   Output: release\XianyuAutoReply\
echo   Zip:    release\%ZIP_NAME%
echo   Run:    release\XianyuAutoReply\XianyuAutoReply.exe
echo ============================================
echo.
echo Notes:
if "%DESKTOP_MODE%"=="remote" (
    echo   1. Server package contains the desktop client and desktop-config.json only
) else (
    echo   1. Local package includes the service sources and Python runtime
)
echo   2. Launcher is compiled to native C code by Nuitka
echo   3. Send release\XianyuAutoReply folder to users
if "%DESKTOP_MODE%"=="remote" (
    echo   4. Share release\%ZIP_NAME% as the server desktop client
) else (
    echo   4. release\%ZIP_NAME% can be uploaded to update server
)
echo   5. desktop-config.json selects the configured local or remote site
echo.
set "BUILD_EXIT_CODE=0"

:end
if defined DESKTOP_CONFIG_TMP if exist "%DESKTOP_CONFIG_TMP%" del /f /q "%DESKTOP_CONFIG_TMP%" >nul 2>&1
if defined DESKTOP_MODE_TMP if exist "%DESKTOP_MODE_TMP%" del /f /q "%DESKTOP_MODE_TMP%" >nul 2>&1
echo.
echo Press any key to exit...
pause >nul
exit /b %BUILD_EXIT_CODE%
