@echo off
setlocal enabledelayedexpansion
title SeparateAudio

rem ---------------------------------------------------------------------
rem Launches the SeparateAudio GUI using the isolated Conda environment
rem "separateaudio". No manual "conda activate" is needed: the environment's
rem interpreter is called directly and its PATH is prepended so bundled
rem FFmpeg and CUDA libraries resolve correctly.
rem ---------------------------------------------------------------------

set "ENV_NAME=separateaudio"
set "PROJECT_DIR=%~dp0"
if "%PROJECT_DIR:~-1%"=="\" set "PROJECT_DIR=%PROJECT_DIR:~0,-1%"
set "CONDA_ROOT="
set "ENV_DIR="

call :find_env
if not defined ENV_DIR (
    echo.
    echo  SeparateAudio could not find the Conda environment "%ENV_NAME%".
    echo.
    echo  Run setup_env.bat in this folder first. It creates the environment
    echo  and installs Python, Demucs and FFmpeg.
    echo.
    pause
    exit /b 1
)

set "ENV_PY=%ENV_DIR%\pythonw.exe"
if not exist "%ENV_PY%" set "ENV_PY=%ENV_DIR%\python.exe"
if not exist "%ENV_PY%" (
    echo.
    echo  Python was not found inside "%ENV_DIR%".
    echo  Run setup_env.bat to repair the environment.
    echo.
    pause
    exit /b 1
)

rem Reproduce what "conda activate" does to PATH so FFmpeg and the CUDA
rem runtime DLLs shipped inside the environment are found.
set "PATH=%ENV_DIR%;%ENV_DIR%\Library\mingw-w64\bin;%ENV_DIR%\Library\usr\bin;%ENV_DIR%\Library\bin;%ENV_DIR%\Scripts;%ENV_DIR%\bin;%PATH%"
set "CONDA_PREFIX=%ENV_DIR%"
set "CONDA_DEFAULT_ENV=%ENV_NAME%"
set "PYTHONIOENCODING=utf-8"
set "PYTHONPATH=%PROJECT_DIR%"

cd /d "%PROJECT_DIR%"
start "" "%ENV_PY%" "%PROJECT_DIR%\App\main.py" %*
exit /b 0

rem ---------------------------------------------------------------------
:find_env
rem 1. Environment variables set by an already-active Conda shell.
if defined CONDA_EXE (
    for %%I in ("%CONDA_EXE%") do set "_D=%%~dpI"
    for %%I in ("!_D!..") do set "_R=%%~fI"
    if exist "!_R!\envs\%ENV_NAME%\python.exe" (
        set "CONDA_ROOT=!_R!"
        set "ENV_DIR=!_R!\envs\%ENV_NAME%"
        exit /b 0
    )
)

rem 2. The usual installation locations.
for %%P in (
    "%USERPROFILE%\anaconda3"
    "%USERPROFILE%\Anaconda3"
    "%USERPROFILE%\miniconda3"
    "%USERPROFILE%\Miniconda3"
    "%USERPROFILE%\miniforge3"
    "%USERPROFILE%\mambaforge"
    "%LOCALAPPDATA%\anaconda3"
    "%LOCALAPPDATA%\miniconda3"
    "%LOCALAPPDATA%\Continuum\anaconda3"
    "%PROGRAMDATA%\anaconda3"
    "%PROGRAMDATA%\Anaconda3"
    "%PROGRAMDATA%\miniconda3"
    "C:\anaconda3"
    "C:\Anaconda3"
    "C:\miniconda3"
) do (
    if exist "%%~P\envs\%ENV_NAME%\python.exe" (
        set "CONDA_ROOT=%%~P"
        set "ENV_DIR=%%~P\envs\%ENV_NAME%"
        exit /b 0
    )
)

rem 3. conda's own registry of environment prefixes.
if exist "%USERPROFILE%\.conda\environments.txt" (
    for /f "usebackq delims=" %%L in ("%USERPROFILE%\.conda\environments.txt") do (
        if exist "%%L\python.exe" (
            for %%N in ("%%L") do (
                if /i "%%~nxN"=="%ENV_NAME%" (
                    set "ENV_DIR=%%L"
                    exit /b 0
                )
            )
        )
    )
)

rem 4. Anything conda on PATH can tell us.
for /f "delims=" %%I in ('where conda 2^>nul') do (
    for %%J in ("%%~dpI..") do (
        if exist "%%~fJ\envs\%ENV_NAME%\python.exe" (
            set "CONDA_ROOT=%%~fJ"
            set "ENV_DIR=%%~fJ\envs\%ENV_NAME%"
            exit /b 0
        )
    )
)
exit /b 1
