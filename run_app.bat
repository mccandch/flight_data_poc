@echo off
rem Opens the SLC/PVU flight browser in your web browser. Close this window to stop it.
cd /d "%~dp0"
rem The daily scan runs on GitHub, so fetch the latest fares.db first (skipped if offline).
git pull --ff-only --quiet
".venv\Scripts\python.exe" -m streamlit run app.py
