$env:TEMP = "D:\NLP\.tmp"
$env:TMP = "D:\NLP\.tmp"
$env:UV_CACHE_DIR = "D:\NLP\.uv_cache"

Write-Host ">>> Using Cache & Temp directory on D: drive to protect C: drive..."
Write-Host "TEMP: $env:TEMP"
Write-Host "UV_CACHE_DIR: $env:UV_CACHE_DIR"

uv pip install --cache-dir D:\NLP\.uv_cache --link-mode copy -r requirements.txt --python .venv\Scripts\python.exe
