$ErrorActionPreference = "Stop"

$backendRoot = Split-Path -Parent $PSScriptRoot
$projectRoot = Split-Path -Parent $backendRoot
Set-Location $backendRoot

if ($env:APP_ENV -ne "test") {
  throw "APP_ENV must be test before starting the messaging stack."
}
if ($env:ALLOW_TEST_FIXTURES -ne "true") {
  throw "ALLOW_TEST_FIXTURES must be true before starting the messaging stack."
}
if ($env:DATABASE_URL -notmatch "127\.0\.0\.1:55433" -or $env:DATABASE_URL -notmatch "luxury_full_cross_platform_e2e_test") {
  throw "DATABASE_URL must target 127.0.0.1:55433/luxury_full_cross_platform_e2e_test."
}

$api = Start-Process -FilePath "python" -ArgumentList "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000" -WorkingDirectory $backendRoot -PassThru -WindowStyle Hidden
$worker = Start-Process -FilePath "python" -ArgumentList "-m", "app.workers.message_worker" -WorkingDirectory $backendRoot -PassThru -WindowStyle Hidden

"FastAPI PID=$($api.Id)"
"MessageWorker PID=$($worker.Id)"
"Stop with: Stop-Process -Id $($api.Id),$($worker.Id)"
