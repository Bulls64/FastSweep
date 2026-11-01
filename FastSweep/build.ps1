# Construit dist\FastSweep.exe : vérifie Python, installe les dépendances manquantes, lance les tests puis PyInstaller.
# Builds dist\FastSweep.exe: checks Python, installs missing dependencies, runs the tests then PyInstaller.
#
# Si PowerShell refuse ce script (« n'est pas signé numériquement »), utilisez plutôt build.bat,
# ou : powershell -ExecutionPolicy Bypass -File .\build.ps1
# If PowerShell refuses this script ("not digitally signed"), use build.bat instead,
# or: powershell -ExecutionPolicy Bypass -File .\build.ps1
param([switch]$SansTests)

$ErrorActionPreference = 'Stop'

function Invoke-Step {
    # Lance une commande native et s'arrête si elle échoue (PowerShell ne le fait pas tout seul).
    # Runs a native command and stops if it fails (PowerShell does not do it by itself).
    param([string]$Message, [string[]]$Command)
    & $Command[0] $Command[1..($Command.Length - 1)]
    if ($LASTEXITCODE -ne 0) { throw $Message }
}

Push-Location $PSScriptRoot
try {
    # Le code de l'application doit être à côté du script (décompression complète de l'archive).
    # The application code must sit next to the script (full extraction of the archive).
    if (-not (Test-Path (Join-Path $PSScriptRoot 'fastsweep\__init__.py'))) {
        throw "Le dossier 'fastsweep' est introuvable dans $PSScriptRoot. Décompressez FastSweep.zip en entier."
    }
    if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
        throw 'Le lanceur Python "py" est introuvable. Installez Python 3.10+ depuis python.org (option "py launcher").'
    }

    & py -c "import PySide6, psutil" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host '[1/3] Installation des dépendances de l''application...'
        Invoke-Step 'Échec de l''installation de requirements.txt.' @('py', '-m', 'pip', 'install', '-r', 'requirements.txt')
    }
    & py -m PyInstaller --version 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Host '[1/3] Installation de PyInstaller...'
        Invoke-Step 'Échec de l''installation de PyInstaller.' @('py', '-m', 'pip', 'install', '-r', 'requirements-dev.txt')
    }

    if ($SansTests) {
        Write-Host '[2/3] Tests ignorés.'
    } else {
        Write-Host '[2/3] Tests...'
        Invoke-Step 'Les tests ont échoué : pas de build (relancez avec -SansTests pour construire quand même).' @('py', '-m', 'unittest', 'discover', '-s', 'tests', '-v')
    }

    Write-Host '[3/3] Construction de l''exécutable...'
    Invoke-Step 'PyInstaller a échoué.' @('py', '-m', 'PyInstaller', '--noconfirm', '--clean', '--distpath', 'dist', '--workpath', 'work', 'FastSweep.spec')

    Write-Host "[OK] $PSScriptRoot\dist\FastSweep.exe"
}
finally {
    Pop-Location
}
