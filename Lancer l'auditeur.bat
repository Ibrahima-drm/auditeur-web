@echo off
chcp 65001 >nul 2>&1
title Auditeur Web
cd /d "%~dp0"

echo.
echo  ============================================
echo      AUDITEUR WEB  -  demarrage
echo  ============================================
echo.

REM --- 1) Trouver Python -------------------------------------------------
set "PYCMD="
py -3 --version >nul 2>&1
if %errorlevel%==0 set "PYCMD=py -3"
if defined PYCMD goto python_ok
python --version >nul 2>&1
if %errorlevel%==0 set "PYCMD=python"
if defined PYCMD goto python_ok

echo  Python n'est pas installe sur cet ordinateur.
echo  Tentative d'installation automatique via winget...
echo.
winget install -e --id Python.Python.3.12 --silent --accept-package-agreements --accept-source-agreements
echo.
echo  --------------------------------------------------------------
echo   Installation de Python terminee.
echo   FERME cette fenetre, puis RELANCE ce fichier (double-clic).
echo  --------------------------------------------------------------
echo.
pause
exit /b

:python_ok
echo  Python detecte :
%PYCMD% --version
echo.

REM --- 2) Environnement isole (venv) ------------------------------------
set "VPY=.venv\Scripts\python.exe"
if exist "%VPY%" goto venv_ok
echo  Creation de l'environnement (premiere utilisation)...
%PYCMD% -m venv .venv
:venv_ok

REM --- 3) Dependances : une seule fois (marqueur .venv\.pret) -----------
if exist ".venv\.pret" goto lancer
echo.
echo  Installation des composants Playwright + navigateur Chromium.
echo  La premiere fois cela telecharge ~150 Mo : patiente quelques minutes...
echo.
"%VPY%" -m pip install --upgrade pip
"%VPY%" -m pip install -r requirements.txt
if errorlevel 1 goto erreur_install
"%VPY%" -m playwright install chromium
if errorlevel 1 goto erreur_install
echo pret> ".venv\.pret"

:lancer
REM --- 4) Lancer l'interface --------------------------------------------
echo.
echo  Ouverture de l'interface dans ton navigateur...
echo  Garde cette fenetre ouverte pendant l'utilisation.
echo.
"%VPY%" interface.py
echo.
echo  Interface fermee.
pause
exit /b

:erreur_install
echo.
echo  ************************************************************
echo   Une erreur est survenue pendant l'installation.
echo   Verifie ta connexion Internet puis relance ce fichier.
echo  ************************************************************
echo.
pause
exit /b