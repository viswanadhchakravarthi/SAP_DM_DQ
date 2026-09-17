@echo off
setlocal

:: Set your credentials here
set "TARGET_USER=admin"
set "TARGET_PASS=1234"

:: Prompt for input
set /p "INPUT_USER=Enter username: "
set /p "INPUT_PASS=Enter password: "

:: Validate credentials
if "%INPUT_USER%"=="%TARGET_USER%" (
    if "%INPUT_PASS%"=="%TARGET_TARGET_PASS%" (
        goto UNLOCK
    )
)

:: Fix for password comparison bug above & auth check
if "%INPUT_USER%"=="%TARGET_USER%" if "%INPUT_PASS%"=="%TARGET_PASS%" goto UNLOCK

echo Authentication failed. Aborting.
pause
exit /b 1

:UNLOCK
echo Credentials confirmed. Deleting target files and directories...

:: Delete directories
if exist logs rmdir /s /q logs
if exist memory_store rmdir /s /q memory_store

:: Delete file
if exist episodic_memory.db del /f /q episodic_memory.db

echo Clean completed successfully.
pause