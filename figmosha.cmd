@echo off
rem  figmosha props sel   instead of   python figmosha.py props sel
rem
rem Resolved from this file's own folder (%~dp0), never the working one, so
rem a copy stays addressable by full path - and a copy put on PATH keeps
rem talking to its own project rather than to whichever one you stand in.
setlocal
set "FIGMOSHA_ROOT=%~dp0"
set "FIGMOSHA_PY=%FIGMOSHA_ROOT%venv\Scripts\python.exe"
if not exist "%FIGMOSHA_PY%" set "FIGMOSHA_PY=python"
"%FIGMOSHA_PY%" "%FIGMOSHA_ROOT%figmosha.py" %*
