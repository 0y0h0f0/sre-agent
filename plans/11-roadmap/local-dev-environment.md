# 本地开发环境与硬件约束

本地开发以 RTX 4060 8GB + 16GB RAM 为基准。该约束决定了 Phase 1 的本地 LLM 选型（见 `phase-1-intelligent-diagnosis.md`）和 Docker 精简启动方式。

## 硬件约束

| 资源 | 规格 | 影响 |
| --- | --- | --- |
| **GPU** | RTX 4060 8GB VRAM | BF16 模型（8B≈16GB）无法装入。默认使用 AWQ/GPTQ 4-bit 量化模型；首推 Qwen2.5-7B-Instruct-AWQ，Qwen3-8B-AWQ 仅作高风险尝试 |
| **RAM** | 16GB | Docker 全栈（12 容器）约 3.5-4GB，常驻后剩余 10GB+，充裕。建议提供 `docker-compose.dev.yml` 精简模式 |
| **推理引擎** | vLLM | 通过 `--max-num-seqs 1 --enforce-eager` 降低显存；Qwen3 thinking 需要额外 reasoning parser 配置 |
| **模型** | Qwen2.5-7B-Instruct-AWQ（默认） | 权重 3.9GB + KV Cache 1.5GB + vLLM 开销 1GB = 6.4GB / 8GB |
| **Context** | 4K tokens | `--max-model-len 4096`，SRE 诊断足够（system + evidence ≤ 3000 tokens） |

## Docker 开发精简模式

16GB 内存跑全栈 12 个容器偏重。提供 `docker-compose.dev.yml` 只启动必要服务，按需追加可观测栈。

```yaml
# docker-compose.dev.yml —— 本地开发最小服务集
services:
  postgres:    # 必需：持久化
  redis:       # 必需：Celery broker + cache
  vllm:        # 必需：本地推理（仅 dev，生产用云端 API）
  api:         # 必需：FastAPI
  worker:      # 必需：Celery 诊断任务
  web:         # 必需：前端
  # 以下按需启动：
  # prometheus + loki + grafana + otel-collector + demo-service + promtail
  # → 需要验证工具层时单独启动：docker compose -f docker-compose.yml up prometheus loki
```

启动命令：

```bash
# 日常开发
docker compose -f docker-compose.dev.yml up -d

# 需要验证真实工具数据时，追加可观测栈
docker compose -f docker-compose.dev.yml -f docker-compose.yml up -d prometheus loki grafana

# 全栈（完整验证）
docker compose up -d
```

参见 `08-deploy/demo-environment.md` 的全栈编排说明；本节是其在受限本地机器上的精简补充。
