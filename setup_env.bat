@echo off
setlocal enabledelayedexpansion
title SeparateAudio - environment setup

rem ---------------------------------------------------------------------
rem Creates (or repairs) the isolated Conda environment "separateaudio"
rem with Python, PyTorch, Demucs and FFmpeg.
rem Safe to re-run: existing packages are simply verified or upgraded.
rem ---------------------------------------------------------------------

set "ENV_NAME=separateaudio"
set "PY_VERSION=3.11"
set "PROJECT_DIR=%~dp0"
set "CONDA_ROOT="

echo ==========================================================
echo  SeparateAudio - environment setup
echo ==========================================================
echo.

rem -- 1. Locate an existing Conda installation -------------------------
call :find_conda
if not defined CONDA_ROOT (
    echo [ERROR] No Anaconda or Miniconda installation was found.
    echo.
    echo Install Anaconda or Miniconda, then run this script again.
    echo Searched: CONDA_EXE, PATH, %%USERPROFILE%%\anaconda3, %%USERPROFILE%%\miniconda3,
    echo           %%PROGRAMDATA%%\anaconda3 and the usual alternatives.
    goto :fail
)

set "CONDA_EXE_PATH=%CONDA_ROOT%\Scripts\conda.exe"
if not exist "%CONDA_EXE_PATH%" set "CONDA_EXE_PATH=%CONDA_ROOT%\condabin\conda.bat"
if not exist "%CONDA_EXE_PATH%" (
    echo [ERROR] Found "%CONDA_ROOT%" but no conda executable inside it.
    goto :fail
)

echo [1/6] Using Conda at: %CONDA_ROOT%
call :resolve_env

rem -- 2. Create the environment ----------------------------------------
if defined ENV_DIR (
    echo [2/6] Environment "%ENV_NAME%" already exists at "%ENV_DIR%" - reusing it.
) else (
    echo [2/6] Creating environment "%ENV_NAME%" with Python %PY_VERSION% ...
    rem conda-forge only: it needs no Terms-of-Service acceptance and carries no
    rem commercial-use restrictions, unlike the Anaconda default channels.
    call "%CONDA_EXE_PATH%" create -n %ENV_NAME% python=%PY_VERSION% -y --override-channels -c conda-forge
    if errorlevel 1 (
        echo [ERROR] Could not create the Conda environment.
        echo         If conda mentions Terms of Service for repo.anaconda.com, this
        echo         script is already avoiding those channels - check your network
        echo         or proxy settings instead.
        goto :fail
    )
    rem A conda installed for all users puts new environments under
    rem %USERPROFILE%\.conda\envs, not under the installation root, so the
    rem location has to be looked up rather than assumed.
    call :resolve_env
)

if not defined ENV_DIR (
    echo [ERROR] The environment was created but could not be located.
    echo         Looked in "%CONDA_ROOT%\envs\%ENV_NAME%",
    echo         "%USERPROFILE%\.conda\envs\%ENV_NAME%" and environments.txt.
    goto :fail
)

set "ENV_PY=%ENV_DIR%\python.exe"
if not exist "%ENV_PY%" (
    echo [ERROR] Python was not found at "%ENV_PY%".
    goto :fail
)
echo       Environment location: %ENV_DIR%

rem -- 3. FFmpeg ---------------------------------------------------------
echo [3/6] Installing FFmpeg (conda-forge) ...
call "%CONDA_EXE_PATH%" install -n %ENV_NAME% -y --override-channels -c conda-forge ffmpeg
if errorlevel 1 (
    echo [WARN] conda could not install FFmpeg. Separation may still work if
    echo        FFmpeg is available elsewhere on PATH.
)

rem -- 4. PyTorch --------------------------------------------------------
echo [4/6] Installing PyTorch ...
echo       Detecting an NVIDIA GPU ...
set "TORCH_CHANNEL=cu124"
where nvidia-smi >nul 2>&1
if errorlevel 1 (
    set "TORCH_CHANNEL=cpu"
    echo       No NVIDIA driver found - installing the CPU build.
) else (
    echo       NVIDIA driver found - installing the CUDA 12.4 build.
)

"%ENV_PY%" -m pip install --upgrade pip setuptools wheel
"%ENV_PY%" -m pip install "numpy<2"
if errorlevel 1 (
    echo [ERROR] Could not install numpy.
    goto :fail
)

rem torch 2.5.1 is the newest release that loads Demucs 4.0.1 checkpoints
rem without the weights_only restriction introduced in torch 2.6.
"%ENV_PY%" -m pip install torch==2.5.1 torchaudio==2.5.1 --index-url https://download.pytorch.org/whl/%TORCH_CHANNEL%
if errorlevel 1 (
    echo [WARN] The %TORCH_CHANNEL% build failed - falling back to the CPU build.
    "%ENV_PY%" -m pip install torch==2.5.1 torchaudio==2.5.1 --index-url https://download.pytorch.org/whl/cpu
    if errorlevel 1 (
        echo [ERROR] Could not install PyTorch.
        goto :fail
    )
)

rem -- 5. Demucs ---------------------------------------------------------
echo [5/6] Installing Demucs and audio helpers ...
"%ENV_PY%" -m pip install demucs==4.0.1 soundfile
if errorlevel 1 (
    echo [ERROR] Could not install Demucs.
    goto :fail
)

rem -- 6. Verify ---------------------------------------------------------
echo [6/6] Verifying the installation ...
rem _probe.py exits non-zero when Demucs, PyTorch, FFmpeg or the model is
rem missing, so errorlevel here is meaningful.
"%ENV_PY%" -I "%PROJECT_DIR%App\_probe.py" htdemucs_ft >"%TEMP%\separateaudio_probe.txt" 2>&1
if errorlevel 1 (
    echo [ERROR] Verification failed - something is still missing:
    findstr /c:"MISSING:" "%TEMP%\separateaudio_probe.txt"
    goto :fail
)
echo       Verification probe: OK

"%ENV_PY%" -c "import demucs, torch, sys; print('python', sys.version.split()[0]); print('torch', torch.__version__, 'cuda', torch.cuda.is_available()); import importlib.metadata as m; print('demucs', m.version('demucs'))"
if errorlevel 1 (
    echo [ERROR] Demucs or PyTorch could not be imported.
    goto :fail
)

"%ENV_DIR%\Library\bin\ffmpeg.exe" -version 2>nul | findstr /i "ffmpeg version"
if errorlevel 1 (
    ffmpeg -version 2>nul | findstr /i "ffmpeg version"
)

echo.
echo ==========================================================
echo  Setup complete. Launch the app with launch.bat
echo ==========================================================
echo.
pause
exit /b 0

rem ---------------------------------------------------------------------
:resolve_env
rem Locate the environment wherever conda actually put it.
set "ENV_DIR="
if exist "%CONDA_ROOT%\envs\%ENV_NAME%\python.exe" (
    set "ENV_DIR=%CONDA_ROOT%\envs\%ENV_NAME%"
    exit /b 0
)
if exist "%USERPROFILE%\.conda\envs\%ENV_NAME%\python.exe" (
    set "ENV_DIR=%USERPROFILE%\.conda\envs\%ENV_NAME%"
    exit /b 0
)
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
exit /b 1

rem ---------------------------------------------------------------------
:find_conda
if defined CONDA_EXE (
    for %%I in ("%CONDA_EXE%") do set "_D=%%~dpI"
    for %%I in ("!_D!..") do set "_R=%%~fI"
    if exist "!_R!\Scripts\conda.exe" (
        set "CONDA_ROOT=!_R!"
        exit /b 0
    )
)
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
    if exist "%%~P\Scripts\conda.exe" (
        set "CONDA_ROOT=%%~P"
        exit /b 0
    )
)
for /f "delims=" %%I in ('where conda 2^>nul') do (
    for %%J in ("%%~dpI..") do (
        if exist "%%~fJ\Scripts\conda.exe" (
            set "CONDA_ROOT=%%~fJ"
            exit /b 0
        )
    )
)
exit /b 1

rem ---------------------------------------------------------------------
:fail
echo.
echo Setup did not complete. Nothing on your system was removed.
echo.
pause
exit /b 1
