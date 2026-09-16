@echo off
chcp 65001 >nul
cd /d D:\ai\llama
llama-server.exe ^
  -m D:\ai\mt\models\Qwen3-8B-Q4_K_M.gguf ^
  --alias qwen3-8b ^
  --host 127.0.0.1 --port 8080 ^
  -c 12288 --parallel 2 ^
  -ngl 99 -fa on ^
  --cache-type-k q8_0 --cache-type-v q8_0 ^
  --jinja ^
  --temp 0.2 --top-p 0.8 --top-k 20