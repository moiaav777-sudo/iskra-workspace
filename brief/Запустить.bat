@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
set "PY="

if exist "python\python.exe" set "PY=python\python.exe"

if not defined PY (
  py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)" >nul 2>nul && set "PY=py -3"
)
if not defined PY (
  python -c "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)" >nul 2>nul && set "PY=python"
)

if not defined PY (
  echo.
  echo  Первый запуск: скачиваю Python для программы, около 11 МБ.
  echo  Это нужно один раз, установка в систему не требуется.
  echo.
  powershell -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference='SilentlyContinue'; [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; [Net.WebRequest]::DefaultWebProxy.Credentials=[Net.CredentialCache]::DefaultNetworkCredentials; Invoke-WebRequest -UseBasicParsing -Uri 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip' -OutFile 'python.zip'; Expand-Archive -Path 'python.zip' -DestinationPath 'python' -Force; Remove-Item 'python.zip'"
  if exist "python\python.exe" set "PY=python\python.exe"
)

if not defined PY (
  echo.
  echo  Не удалось подготовить Python автоматически - вероятно, интернет закрыт.
  echo  Установите Python 3 с сайта https://www.python.org/downloads/
  echo  при установке отметьте "Add python.exe to PATH", затем запустите этот файл снова.
  echo.
  pause
  exit /b 1
)

%PY% app\server.py
if errorlevel 1 pause
