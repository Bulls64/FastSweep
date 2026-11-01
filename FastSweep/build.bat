@echo off
REM Construit dist\FastSweep.exe : vérifie Python, installe les dépendances manquantes, lance les tests puis PyInstaller.
REM Builds dist\FastSweep.exe: checks Python, installs missing dependencies, runs the tests then PyInstaller.
REM
REM Un .bat n'est pas soumis à la stratégie d'exécution de PowerShell : double-clic ou "build.bat" dans un terminal.
REM A .bat is not subject to the PowerShell execution policy: double-click it or run "build.bat" in a terminal.
REM Option : "build.bat --sans-tests" saute les tests / Option: "build.bat --sans-tests" skips the tests.
setlocal
chcp 65001 >nul
cd /d "%~dp0"

REM Le code de l'application doit être à côté du script (décompression complète de l'archive).
REM The application code must sit next to the script (full extraction of the archive).
if not exist "fastsweep\__init__.py" (
    echo [ERREUR] Le dossier "fastsweep" est introuvable dans %CD%
    echo          Décompressez FastSweep.zip en entier : ce dossier doit contenir fastsweep, tests, main.py et build.bat.
    goto :fail
)

where py >nul 2>&1
if errorlevel 1 (
    echo [ERREUR] Le lanceur Python "py" est introuvable. Installez Python 3.10 ou plus depuis python.org
    echo          en cochant "py launcher", puis rouvrez le terminal.
    goto :fail
)

py -c "import PySide6, psutil" >nul 2>&1
if errorlevel 1 (
    echo [1/3] Installation des dépendances de l'application...
    py -m pip install -r requirements.txt
    if errorlevel 1 goto :fail
)

py -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo [1/3] Installation de PyInstaller...
    py -m pip install -r requirements-dev.txt
    if errorlevel 1 goto :fail
)

if /i "%~1"=="--sans-tests" (
    echo [2/3] Tests ignorés.
) else (
    echo [2/3] Tests...
    py -m unittest discover -s tests -v
    if errorlevel 1 (
        echo.
        echo [ERREUR] Les tests ont échoué : pas de build. Pour construire quand même : build.bat --sans-tests
        goto :fail
    )
)

echo [3/3] Construction de l'exécutable...
py -m PyInstaller --noconfirm --clean --distpath dist --workpath work FastSweep.spec
if errorlevel 1 goto :fail

echo.
echo [OK] %CD%\dist\FastSweep.exe
if not defined FASTSWEEP_NO_PAUSE pause
exit /b 0

:fail
echo.
echo [ÉCHEC] Voir le message d'erreur ci-dessus.
if not defined FASTSWEEP_NO_PAUSE pause
exit /b 1
