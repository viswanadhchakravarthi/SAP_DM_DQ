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

:: This wipes the DISPOSABLE runtime state under storage\: perm = episodic DB + vector index +
:: handoff outbox, tmp = logs. To drop only the truly throwaway part, delete storage\tmp instead.
if exist storage\tmp rmdir /s /q storage\tmp
if exist storage\perm rmdir /s /q storage\perm

:: NOT deleted here, on purpose - these moved OUT of storage\ and are version-controlled now:
::   rules\local\clients\<id>\   per-client duplicate rules, column mappings, remembered decisions
::   rules\procedural\           promoted skill registry and shared duplicate rules
::   data\clients\<id>\          uploaded client CSVs and workspace.json
:: Deleting them is a git operation, not a cache wipe: remove the folder and commit, or
:: `git checkout -- rules/` to get the committed rules back. Wiping storage\ alone leaves the
:: agent with its learned rules but no run history, which is usually what you want.

:: Leftovers from before the storage\ layout (a first start of the new layout moves these).
if exist logs rmdir /s /q logs
if exist memory_store rmdir /s /q memory_store
if exist handoff rmdir /s /q handoff
if exist client_data rmdir /s /q client_data
if exist episodic_memory.db del /f /q episodic_memory.db

echo Clean completed successfully.
pause