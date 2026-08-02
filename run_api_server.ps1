$ErrorActionPreference = "Stop"

Set-Location -LiteralPath $PSScriptRoot

if (-not (Test-Path -LiteralPath ".\.venv\Scripts\Activate.ps1")) {
    python -m venv .venv
}

.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt

if (-not $env:DRIVE_DATA_S3_BUCKET) {
    $env:DRIVE_DATA_S3_BUCKET = Read-Host "S3 bucket name"
}

if (-not $env:DRIVE_DATA_S3_PREFIX) {
    $prefix = Read-Host "S3 prefix [drive-logs]"
    if ([string]::IsNullOrWhiteSpace($prefix)) {
        $prefix = "drive-logs"
    }
    $env:DRIVE_DATA_S3_PREFIX = $prefix
}

if (-not $env:AWS_DEFAULT_REGION -and -not $env:AWS_REGION) {
    $region = Read-Host "AWS region [ap-northeast-2]"
    if ([string]::IsNullOrWhiteSpace($region)) {
        $region = "ap-northeast-2"
    }
    $env:AWS_DEFAULT_REGION = $region
}

if ($env:AWS_ACCESS_KEY_ID) {
    $maskedKey = if ($env:AWS_ACCESS_KEY_ID.Length -gt 8) {
        "$($env:AWS_ACCESS_KEY_ID.Substring(0, 4))...$($env:AWS_ACCESS_KEY_ID.Substring($env:AWS_ACCESS_KEY_ID.Length - 4))"
    } else {
        "<set>"
    }
    $replaceKey = Read-Host "AWS Access Key ID is already set ($maskedKey). Replace it? [y/N]"
    if ($replaceKey -match "^[Yy]") {
        Remove-Item Env:\AWS_ACCESS_KEY_ID -ErrorAction SilentlyContinue
        Remove-Item Env:\AWS_SECRET_ACCESS_KEY -ErrorAction SilentlyContinue
        Remove-Item Env:\AWS_SESSION_TOKEN -ErrorAction SilentlyContinue
    }
}

if (-not $env:AWS_ACCESS_KEY_ID) {
    $env:AWS_ACCESS_KEY_ID = Read-Host "AWS Access Key ID"
}

if (-not $env:AWS_SECRET_ACCESS_KEY) {
    $secret = Read-Host "AWS Secret Access Key" -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secret)
    try {
        $env:AWS_SECRET_ACCESS_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
}

if (-not $env:AWS_SESSION_TOKEN) {
    $sessionToken = Read-Host "AWS Session Token [optional; only for STS temporary credentials, press Enter to skip]"
    if (-not [string]::IsNullOrWhiteSpace($sessionToken)) {
        if ($sessionToken -match "^(AKIA|ASIA)[A-Z0-9]{16}$") {
            Write-Host "That looks like an AWS Access Key ID, not a Session Token. Skipping session token."
        } else {
            $env:AWS_SESSION_TOKEN = $sessionToken
        }
    }
}

Write-Host ""
Write-Host "Starting API server: http://127.0.0.1:8000"
Write-Host "Bucket: $env:DRIVE_DATA_S3_BUCKET"
Write-Host "Prefix: $env:DRIVE_DATA_S3_PREFIX"
if ($env:AWS_DEFAULT_REGION) {
    Write-Host "Region: $env:AWS_DEFAULT_REGION"
} elseif ($env:AWS_REGION) {
    Write-Host "Region: $env:AWS_REGION"
}
Write-Host ""

uvicorn api_server:app --host 127.0.0.1 --port 8000
