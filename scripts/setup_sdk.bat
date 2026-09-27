@echo off
setlocal
rem ==========================================================
rem  Fetch the Spider_XHS SDK and apply this project's fixes.
rem  Usage: scripts\setup_sdk.bat   (path-independent)
rem ==========================================================
cd /d "%~dp0.."

if exist Spider_XHS\.git (
    echo [skip] Spider_XHS already present.
) else (
    echo [1/2] Cloning Spider_XHS ...
    git clone --depth 1 https://github.com/cv-cat/Spider_XHS.git Spider_XHS
    if errorlevel 1 (
        echo [error] git clone failed. Check your network ^(proxy / VPN^) and retry.
        pause
        exit /b 1
    )
)

echo [2/2] Applying fixes patch ...
pushd Spider_XHS
git apply --reverse --check ..\patches\spider_xhs_fixes.patch >nul 2>&1
if %errorlevel%==0 (
    echo [skip] Patch already applied.
) else (
    git apply ..\patches\spider_xhs_fixes.patch
    if errorlevel 1 (
        echo [error] Failed to apply patch. Inspect patches\spider_xhs_fixes.patch.
        popd
        pause
        exit /b 1
    )
    echo [done] Patch applied.
)
popd

echo.
echo All set. Next: create a venv and install dependencies, see README.md
pause
