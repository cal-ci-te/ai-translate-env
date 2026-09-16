# 本地 AI 翻译环境

全本地运行的网页翻译方案：**llama.cpp + Qwen3-8B + 自建网关 + 沉浸式翻译**

- 隐私自持：所有文本不出本机
- 零 API 费用、无限量
- 代码块 100% 保真（逐字节）
- 行内标识符（`--flag` / `API_KEY` / `v1.2.3`）不被翻译
- 页级术语记忆，同类文档译法自动趋同

## 文档索引

| 文档 | 内容 |
|---|---|
| [01-启动方式.md](01-启动方式.md) | 日常启动、停止、健康检查、排错 |
| [02-翻译配置.md](02-翻译配置.md) | 沉浸式翻译插件的全部配置项 |
| [03-项目配置.md](03-项目配置.md) | 目录结构、模型参数、网关设计、已知问题 |

## 一页速查

**启动（顺序不能反）**

1. 双击 `D:\ai\mt\start-model.bat` → 等 `listening on http://127.0.0.1:8080`
2. 双击 `D:\ai\mt\start-gateway.bat` → 等 `gateway on http://127.0.0.1:8000`
3. 浏览器里用沉浸式翻译（两个黑窗口最小化，别关）

**健康检查**

```powershell
curl.exe -s http://127.0.0.1:8000/health
nvidia-smi --query-gpu=memory.used,memory.free --format=csv
```

预期：`{"ok":true,...}` / `used ≈ 6340 MiB`

**关键路径**

| 项 | 路径 |
|---|---|
| 推理引擎 | `D:\ai\llama\llama-server.exe` |
| 模型 | `D:\ai\mt\models\Qwen3-8B-Q4_K_M.gguf` |
| 网关 | `D:\ai\mt\local-mt-gateway.py` |
| Python | `D:\Python\Python313\python.exe` |
| 日志 | `D:\ai\mt\logs\gateway.log` |

**服务端口**

| 端口 | 服务 |
|---|---|
| 8080 | llama-server（模型推理） |
| 8000 | 网关（给插件调用） |

## 硬件与软件基线

| 项 | 值 |
|---|---|
| 显卡 | NVIDIA GeForce RTX 4060 Laptop（8187 MiB，可用约 7099 MiB） |
| 驱动 | 552.46 / CUDA 12.4 |
| 推理引擎 | llama.cpp b10992（win-cuda-12.4） |
| 模型 | Qwen3-8B-Q4_K_M（5,027,783,488 字节） |
| 模型 sha256 | `d98cdcbd03e17ce47681435b5150e34c1417f50b5c0019dd560e4882c5745785` |
| Python | 3.13（`D:\Python\Python313`） |
