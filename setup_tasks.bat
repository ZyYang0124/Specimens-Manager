@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 正在配置每日自动备份（每天 03:00，当前用户，无需管理员权限）...
for /f "delims=" %%P in ('where python') do set PY=%%P
schtasks /create /tn "SpecimenManagerBackup" /tr "\"%PY%\" \"%~dp0backup.py\"" /sc daily /st 03:00 /f
if errorlevel 1 (
  echo 计划任务创建失败。
) else (
  echo 已创建每日备份计划任务 SpecimenManagerBackup
)
echo.
echo 正在配置开机自启动（写入当前用户注册表 Run 键）...
reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v SpecimenManager /t REG_SZ /d "%~dp0autostart.bat" /f
echo.
echo 配置完成：开机自动启动服务（后台运行），每天 03:00 自动备份数据库。
echo 如需取消，运行 uninstall_tasks.bat
pause
