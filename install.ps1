$ErrorActionPreference = "Stop"

# Load env vars from .env if present
$envFile = ".env"
if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        $line = $_.Trim()
        if (-not $line -or $line.StartsWith("#")) { return }
        $parts = $line -split "=", 2
        if ($parts.Count -eq 2) {
            $name  = $parts[0].Trim()
            $value = $parts[1].Trim().Trim("'`"")  # trim quotes
            if ($name) { [System.Environment]::SetEnvironmentVariable($name, $value, "Process") }
        }
    }
}

if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
    throw "Python launcher 'py' was not found. Install Python 3 first."
}

$venvPath = ".venv"

if (-not (Test-Path $venvPath)) {
    py -m venv $venvPath
}

& .\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

Write-Host "Installed requirements into .venv"
Write-Host "Activate later with: .\.venv\Scripts\Activate.ps1"