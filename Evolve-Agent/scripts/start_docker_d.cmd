@echo off
setlocal
if not exist "D:\Docker\Docker\Docker Desktop.exe" (
  echo Docker Desktop was not found in D:\Docker\Docker.
  exit /b 1
)
if not exist "D:\DockerSettings\Roaming" mkdir "D:\DockerSettings\Roaming"
if not exist "D:\DockerSettings\Local" mkdir "D:\DockerSettings\Local"
set "APPDATA=D:\DockerSettings\Roaming"
set "LOCALAPPDATA=D:\DockerSettings\Local"
start "" "D:\Docker\Docker\Docker Desktop.exe"
endlocal
