$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$log = Join-Path $PSScriptRoot 'setup-log.txt'
Start-Transcript -Path $log -Force | Out-Null
try {
    Write-Host 'Game Chat Translator - automatic setup'
    $runtimeRoot = Join-Path $env:LOCALAPPDATA 'GameChatTranslator\Python311'
    $runtimePython = Join-Path $runtimeRoot 'python.exe'
    $venvPython = Join-Path $PSScriptRoot '.venv311\Scripts\python.exe'
    $ready = Join-Path $PSScriptRoot '.venv311\ready-v3.txt'
    function Test-Python311([string]$exe, [string[]]$prefix) {
        try {
            & $exe @prefix -c "import sys, tkinter, venv; sys.exit(0 if sys.version_info[:2] == (3,11) and sys.maxsize > 2**32 else 1)" 2>$null | Out-Null
            return ($LASTEXITCODE -eq 0)
        } catch { return $false }
    }
    if (-not (Test-Python311 $venvPython @())) {
        $baseExe = $null
        $baseArgs = @()
        if (Test-Python311 $runtimePython @()) {
            $baseExe = $runtimePython
        } elseif (Test-Python311 'py' @('-3.11')) {
            $baseExe = 'py'
            $baseArgs = @('-3.11')
        } else {
            Write-Host 'Preparing Python 3.11. Your existing Python installation is kept.'
            $managerOK = $false
            # The new Python Install Manager can install a compatible runtime.
            # Classic launchers will fail harmlessly and use the official installer below.
            try {
                & py install 3.11
                $managerOK = ($LASTEXITCODE -eq 0) -and (Test-Python311 'py' @('-3.11'))
            } catch { $managerOK = $false }
            if ($managerOK) {
                $baseExe = 'py'
                $baseArgs = @('-3.11')
            } else {
                Write-Host 'Downloading the official Python 3.11 installer...'
                [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
                $installer = Join-Path $env:TEMP ('game-translator-python-' + [guid]::NewGuid().ToString() + '.exe')
                try {
                    Invoke-WebRequest -UseBasicParsing -Uri 'https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe' -OutFile $installer
                    $signature = Get-AuthenticodeSignature -FilePath $installer
                    if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notlike '*Python Software Foundation*') {
                        throw 'The Python installer signature could not be verified.'
                    }
                    $installArgs = '/quiet InstallAllUsers=0 Include_launcher=0 Include_tcltk=1 Include_pip=1 Include_test=0 Include_doc=0 AssociateFiles=0 PrependPath=0 Shortcuts=0 TargetDir="' + $runtimeRoot + '"'
                    $proc = Start-Process -FilePath $installer -ArgumentList $installArgs -Wait -PassThru
                    if ($proc.ExitCode -notin @(0, 3010) -or -not (Test-Python311 $runtimePython @())) {
                        throw ('Python setup failed with code ' + $proc.ExitCode)
                    }
                } finally {
                    if (Test-Path -LiteralPath $installer) { Remove-Item -LiteralPath $installer -Force }
                }
                $baseExe = $runtimePython
            }
        }
        Write-Host 'Creating the translator environment...'
        & $baseExe @baseArgs -m venv (Join-Path $PSScriptRoot '.venv311')
        if ($LASTEXITCODE -ne 0) { throw 'Could not create the Python environment.' }
        if (Test-Path -LiteralPath $ready) { Remove-Item -LiteralPath $ready -Force }
    }
    if (-not (Test-Path -LiteralPath $ready)) {
        Write-Host 'Installing OCR and translation packages. Please wait...'
        & $venvPython -m pip install --upgrade pip
        if ($LASTEXITCODE -ne 0) { throw 'Could not update pip.' }
        & $venvPython -m pip install -r (Join-Path $PSScriptRoot 'requirements.txt')
        if ($LASTEXITCODE -ne 0) { throw 'Package installation failed.' }
        & $venvPython -c "import tkinter, mss, numpy, rapidocr_onnxruntime, deep_translator, dxcam"
        if ($LASTEXITCODE -ne 0) { throw 'Package check failed.' }
        Set-Content -LiteralPath $ready -Value 'ready'
    }
    Write-Host 'Opening the translator...'
    & $venvPython (Join-Path $PSScriptRoot 'launcher.py')
    if ($LASTEXITCODE -ne 0) { throw 'Translator exited with an error.' }
} catch {
    Write-Host ('ERROR: ' + $_.Exception.Message) -ForegroundColor Red
    Write-Host 'Please send setup-log.txt from this folder.'
    Stop-Transcript | Out-Null
    exit 1
}
Stop-Transcript | Out-Null
exit 0
