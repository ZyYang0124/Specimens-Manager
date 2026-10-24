@echo off
chcp 65001 >nul
title 标本信息管理系统
cd /d "%~dp0"
echo 正在启动标本信息管理系统，请稍候...
python server.py --open
if errorlevel 1 (
  echo.
  echo 启动失败：请确认已安装 Python 3.10+ 并执行过:
  echo     pip install -r requirements.txt
  echo     python seed.py
  pause
)
