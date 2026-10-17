@echo off
chcp 65001 >nul
cd /d "%~dp0"
where npm >nul 2>nul || (echo 没找到 Node.js，请先安装 https://nodejs.org & pause & exit /b 1)
if not exist node_modules (
  echo 第一次启动，正在安装依赖，需要联网，几分钟...
  call npm install || (pause & exit /b 1)
)
call npm run build || (pause & exit /b 1)
start "" "%~dp0node_modules\electron\dist\electron.exe" "%~dp0."
