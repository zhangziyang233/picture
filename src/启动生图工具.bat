@echo off
rem 生图工具启动脚本（Windows）
rem 说明：本文件已移除所有本机专属路径。
rem 如需指定解释器，请在运行前设置环境变量 PYTHONW_EXE，
rem 例如：set "PYTHONW_EXE=C:\Path\To\pythonw.exe"
setlocal
set "PY="

if defined PYTHONW_EXE if exist "%PYTHONW_EXE%" set "PY=%PYTHONW_EXE%"

if not defined PY (
  for /f "delims=" %%i in ('where pythonw 2^>nul') do (
    if not defined PY set "PY=%%i"
  )
)

if not defined PY if exist "%USERPROFILE%\anaconda3\pythonw.exe" set "PY=%USERPROFILE%\anaconda3\pythonw.exe"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe"

if not defined PY (
  echo 未找到 pythonw.exe。
  echo 请安装带 tkinter 与 Pillow 的 Python，或设置环境变量 PYTHONW_EXE 指向 pythonw.exe 后重试。
  pause
  exit /b 1
)

start "" "%PY%" "%~dp0agnes_gui.pyw"
