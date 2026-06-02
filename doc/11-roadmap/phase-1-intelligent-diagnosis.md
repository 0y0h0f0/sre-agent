# Phase 1：智能诊断升级（核心能力）

目标：把当前基于 FakeLLM 固定规则的"诊断"升级为真实 LLM 推理，从 Demo 到有用。这是优先级最高的阶段，对应 `00-overview/milestones.md` 之后的第一个演进里程碑。本阶段与 `02-agent/langgraph-workflow.md` 的节点结构紧耦合。

> **Phase 1 完成边界**：以真实 provider smoke 通过为准。`llm_provider=fake`、`httpx.MockTransport`、纯单元测试和文档状态只能证明代码路径落地，不能证明 Phase 1 完成。完成验收至少需要一个真实 provider（本地 `vllm` 或云端 API）跑通端到端诊断 smoke，并产出 provider/model/token metadata、结构化 rationale、evidence_id 引用、延迟和成本/资源记录。

## 1.1 LLM 分层策略

> **实现状态（代码已落地，Phase 1 未验收）**：Provider 抽象层已实现，`_build_deps()` 不再硬编码 `FakeLLM()`。
> - `packages/agent/llm/base.py` — `LLMProvider` Protocol + `LLMCallMetadata` + JSON 解析helper
> - `packages/agent/llm/fake_adapter.py` — `FakeLLMAdapter`（迁移 FakeLLM，保持测试确定性，记录元数据）
> - `packages/agent/llm/openai_adapter.py` — `OpenAICompatibleAdapter`（vLLM 本地 / OpenAI / DeepSeek）
> - `packages/agent/llm/anthropic_adapter.py` — `AnthropicAdapter`（Claude Messages API，adaptive thinking）
> - `packages/agent/llm/factory.py` — `build_llm(settings)` 按 `llm_provider` 选择 adapter
> - settings 新增：`llm_base_url`、`llm_api_key`、`llm_timeout_seconds`、`llm_max_tokens`、`llm_temperature`、`llm_reasoning_enabled`、`llm_reasoning_effort`
> - 测试：`tests/unit/test_llm_providers.py`（35 例，全部离线，网络 adapter 用 `httpx.MockTransport`）
>
> 待真实环境验证（本地 sandbox 无 GPU/网络无法跑）：`vllm` + Qwen2.5-7B-AWQ 端到端 smoke、云端 API 单次成本 < $0.05、深度推理实测延迟。这些不阻塞 1.1 的 adapter 代码落地，但会阻塞 Phase 1 完成验收。

**核心原则**：开发测试默认用 Qwen2.5-7B-Instruct-AWQ 本地验证流程；Qwen3-8B-AWQ 作为可选高风险验证项。生产环境按部署条件选择云端 API 或高参数本地模型。LLM Provider 抽象层的目标是配置切换，不承诺在未实现 provider factory 前"零代码改动"。

### 方案 A：本地开发测试 —— vLLM + 7B/8B AWQ 量化

定位：RTX 4060 8GB 上零成本本地验证，确保 LangGraph 流程、Prompt 模板、证据注入逻辑跑通（硬件约束见 `local-dev-environment.md`）。

关键策略：BF16 模型（8B≈16GB）无法装入 8GB VRAM。必须使用 AWQ 4-bit 量化模型，将权重压缩到 ~4-5GB，为 KV Cache 和 vLLM 运行时留出空间。

**模型选型优先级**

| 优先级 | 模型 | 量化方式 | 权重大小 | KV Cache (4K) | vLLM 开销 | 合计 | 8GB 可行性 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| **首推** | Qwen2.5-7B-Instruct-AWQ | AWQ 4-bit | ~3.9 GB | ~1.5 GB | ~1 GB | ~6.4 GB | 充裕 |
| 备选 | Qwen3-8B-AWQ | AWQ 4-bit | ~5.0 GB | ~1.8 GB | ~1 GB | ~7.8 GB | 极其紧张，需实测 |
| 降级 | Qwen2.5-7B-Instruct-GPTQ-Int4 | GPTQ 4-bit | ~4.0 GB | ~1.5 GB | ~1 GB | ~6.5 GB | 充裕 |
| 兜底 | Qwen3-4B-AWQ | AWQ 4-bit | ~2.5 GB | ~0.8 GB | ~1 GB | ~4.3 GB | 绰绰有余 |

> 结论：首推 Qwen2.5-7B-Instruct-AWQ（成熟稳定，8GB 余量更合理）。Qwen3-8B-AWQ 已可用，但 7.8GB / 8GB 几乎没有余量；如果要验证 thinking 模式，需要额外配置 vLLM reasoning parser。

**vLLM 低显存启动参数**

```yaml
# docker-compose.yml 新增
vllm:
  image: vllm/vllm-openai:latest
  ports:
    - "8001:8000"
  volumes:
    - ./models:/models:ro
  command: >
    --model /models/Qwen2.5-7B-Instruct-AWQ
    --served-model-name qwen-7b
    --max-model-len 4096
    --max-num-seqs 1
    --gpu-memory-utilization 0.88
    --enforce-eager
    --dtype auto
  deploy:
    resources:
      reservations:
        devices:
          - driver: nvidia
            count: 1
            capabilities: [gpu]
```

| 参数 | 值 | 理由 |
| --- | --- | --- |
| `--max-model-len 4096` | 4K | 8GB 只能支撑 4K context，8K 会 OOM |
| `--max-num-seqs 1` | 单请求 | 开发测试无需并发，单请求显著降低 KV Cache 预分配 |
| `--gpu-memory-utilization 0.88` | 88% | 留 12% 余量应对 CUDA context / NCCL 开销 |
| `--enforce-eager` | 禁用 CUDA Graph | CUDA Graph 额外占用 ~0.5GB，开发环境不需要 |
| `--dtype auto` | 自动 | 按模型 config / vLLM 版本自动选择；如启动失败再显式尝试 `--dtype half` |

不开启的参数：`--enable-prefix-caching`（可能增加显存占用，8GB 先关闭）、`--tensor-parallel-size`（单卡不需要）、`--enable-chunked-prefill`（单请求不需要）。

**模型下载**

```bash
# 方式 1：HuggingFace（需网络）
huggingface-cli download Qwen/Qwen2.5-7B-Instruct-AWQ --local-dir ./models/Qwen2.5-7B-Instruct-AWQ

# 方式 2：ModelScope（国内更快）
modelscope download Qwen/Qwen2.5-7B-Instruct-AWQ --local_dir ./models/Qwen2.5-7B-Instruct-AWQ
```

**验证**

```bash
curl http://localhost:8001/v1/models
# → {"data":[{"id":"qwen-7b","object":"model",...}]}

curl http://localhost:8001/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen-7b","messages":[{"role":"user","content":"什么是数据库连接池耗尽?"}],"max_tokens":256}'
# → 预期 3-5 秒返回，VRAM 峰值 < 7.5GB

watch -n1 nvidia-smi   # 实时监控 VRAM
```

**切换到 Qwen3-8B-AWQ 的条件**：仅在 Qwen2.5-7B-AWQ 流程跑通后尝试，替换模型配置并开启 reasoning parser：

```bash
--model /models/Qwen3-8B-AWQ
--served-model-name qwen3-8b
--gpu-memory-utilization 0.92   # 调高到 92%，搏一把
--enable-reasoning
--reasoning-parser deepseek_r1
```

如果 OOM，回退到 Qwen2.5-7B-AWQ。开发测试阶段优先验证 LangGraph 流程、Prompt 模板和 JSON 输出稳定性，不把 7B vs 8B 的诊断质量差异作为本地验收依据。

> 用途：Prompt 迭代调试、LangGraph 流程验证、前端联调、开发阶段单元/集成测试。**仅用于流程验证，不用于诊断能力评估——真实效果以云端 API（方案 B）或高参数模型（方案 C）为准。**

### 方案 B：生产 —— 云端 LLM API

定位：按需选用高能力模型，无需自建 GPU 集群。

| 提供商 | 模型 | 适用场景 |
| --- | --- | --- |
| **Anthropic Claude** | claude-sonnet-4-6 / claude-opus-4-8 | 首选，SRE 诊断推理能力强；Claude 4.6+ 优先用 adaptive thinking |
| **DeepSeek** | deepseek-v4-flash / deepseek-v4-pro | 高性价比备选，中文能力强；`deepseek-chat` / `deepseek-reasoner` 仅作兼容 alias，计划 2026-07-24 弃用 |
| **OpenAI** | 按实施时最新 GPT/Reasoning 模型清单选择 | 备选，生态成熟；不要在计划中长期绑定已被后续模型替代的旧模型名 |

**Anthropic Prompt Caching 策略**（降成本关键）：

```text
System Prompt + Runbook Chunks（固定前缀） → cache_control: {type: "ephemeral"}
Evidence Data（动态） → 不缓存，每次不同
```

注意边界：Prompt Caching 命中不是免费，而是按 cache hit 价格计费；默认 TTL 为 5 分钟，1 小时 TTL 价格更高。缓存还要求达到对应模型的最小可缓存 token 数，System Prompt 单独 500 tokens 通常不足以形成有效缓存点，应把固定 system + runbook 前缀合并规划。

单次诊断成本估算（Claude Sonnet 4.6，缓存命中后）：

- 固定前缀 ~1500 tokens cache hit → $0.30/MTok × 0.0015 = $0.00045
- Evidence ~2000 tokens cache miss → $3/MTok × 0.002 = $0.006
- Output ~500 tokens → $15/MTok × 0.0005 = $0.0075
- **缓存命中后合计 ~$0.014/次诊断**；首次 cache write 会更贵，约 ~$0.019/次

### 方案 C：生产 —— 本地高参数模型

定位：数据不出内网，自建 GPU 推理集群。

| 模型 | 参数量 | 显存需求 | 推理引擎 | 说明 |
| --- | --- | --- | --- | --- |
| Qwen3-235B-A22B | 235B (22B active) | 需按量化、context、并发重新容量评估 | vLLM / SGLang | MoE 激活参数少，但权重仍需常驻显存；不要只按 22B active 估算显存 |
| DeepSeek-V3-0324 | 671B (37B active) | 需多卡数据中心 GPU，取决于 FP8/INT4/并发 | vLLM / SGLang | MoE，671B 总参数，37B 激活；0324 是旧 checkpoint，实施前确认是否已被 V4/V3.x 替代 |
| Qwen3-32B-Instruct | 32B | 1-2×A100 80GB，取决于 context 与量化 | vLLM | Dense 模型，容量估算更直接 |
| Llama-4-Maverick-17B | 17B | 实施前二次确认模型可用性、license、推理引擎支持 | vLLM | 备选候选，不作为默认路线 |

选型建议：追求质量 → 优先评估最新 DeepSeek / Claude / Qwen 高参数模型；追求性价比 → Qwen3-32B 或云端高性价比 API；硬件受限 → 不要把 MoE active 参数等同于显存需求，必须用目标 context 和并发做容量测试。

### LLM Provider 抽象层设计

目标：一套 provider factory，三种后端（vLLM 本地 / 云端 API / 本地高参数），由配置选择适配器。当前代码里的 worker 仍固定实例化 `FakeLLM()`，节点也调用同步 `generate_json()` / `invoke()`；因此 P1 的第一步不是直接加云端 API，而是先把 FakeLLM 抽成同一协议下的一个 adapter。

```python
# packages/agent/llm/base.py
class LLMProvider(Protocol):
    """兼容当前 LangGraph 同步节点的提供商协议。"""
    def invoke(self, messages: list[dict[str, Any]], *, thinking: bool = False, **kwargs: Any) -> str:
        ...

    def generate_json(self, prompt: str, output_schema: Any, *, thinking: bool = False, **kwargs: Any) -> Any:
        ...

class LLMCallMetadata(TypedDict, total=False):
    model: str
    provider: str
    usage: dict[str, int]              # prompt_tokens, completion_tokens
    reasoning_summary: str             # 可审计摘要，不默认持久化原始 chain-of-thought
    finish_reason: str

# packages/agent/llm/factory.py          → 根据 settings.llm_provider 构造 adapter
# packages/agent/llm/fake_adapter.py     → 迁移当前 FakeLLM，保持测试确定性
# packages/agent/llm/vllm_adapter.py     → 方案 A/C，OpenAI-compatible API
# packages/agent/llm/anthropic_adapter.py → 方案 B (Claude)
# packages/agent/llm/openai_adapter.py    → 方案 B (OpenAI/DeepSeek 等兼容 API)
```

实现边界：若后续要支持 streaming 或真正 async graph，再单独引入 `async chat()`；不要在第一步同时改 provider、节点同步模型和流式 UI。

**配置切换**（`packages/common/settings.py`）：

```python
# 本地开发（RTX 4060 8GB）
llm_provider: str = "vllm"
llm_base_url: str = "http://vllm:8000/v1"
llm_model: str = "qwen-7b"             # --served-model-name
llm_api_key: str | None = None
llm_max_tokens: int = 512              # 4K context 下限制输出长度
llm_temperature: float = 0.1
llm_reasoning_enabled: bool = False
llm_reasoning_effort: str = "medium"   # low | medium | high，按 provider 映射

# 生产（云端 API）
# llm_provider = "anthropic"
# llm_api_key = "sk-ant-xxx"
# llm_model = "claude-sonnet-4-6"
# llm_reasoning_enabled = True

# 生产（本地高参数 GPU 集群）
# llm_provider = "vllm"
# llm_base_url = "http://gpu-cluster:8000/v1"
# llm_model = "deepseek-v3-0324"
```

**验收标准**：

- `apps.worker.tasks._build_deps()` 不再硬编码 `FakeLLM()`，而是通过 provider factory 构造 adapter。
- `llm_provider=fake` 下所有现有单元/集成测试保持确定性通过。
- **Phase 1 完成门槛**：至少一个真实 provider smoke 通过；`llm_provider=vllm` + Qwen2.5-7B-AWQ 下，4 类 smoke eval 能完成端到端诊断，或云端 provider 跑通等价 4 类 smoke。Qwen3-8B-AWQ 仅作为可选显存验证。
- Cloud API 方案下单次诊断成本 < $0.05，成本计算包含 cache hit 费用、首次 cache write 和失败重试。
- 本地高参数方案下 diagnose 节点延迟 < 30s，并记录 prompt_tokens / completion_tokens / provider / model。

## 1.2 推理深度分层调度

> **实现状态（代码已落地，待真实 provider smoke 验证）**：每节点推理深度由配置驱动，可整体关闭以加速本地迭代。
> - `packages/agent/llm/reasoning.py` — `should_use_deep_reasoning(settings, node)`、`deep_reasoning_nodes()`、LLM 调用审计 `capture_metadata()` / `record_llm_call()` / `format_call_metadata()`
> - settings 新增 `llm_reasoning_nodes`（逗号分隔，默认 `diagnose`），配合已有 `llm_reasoning_enabled` 主开关
> - `diagnose` 节点：按配置传 `thinking=`，产出结构化可审计 rationale（`state["diagnosis_rationale"]`，引用 evidence_id + 排序理由 + root cause 选择 + missing_evidence），并把 provider/model/token 记入 `state["llm_calls"]` 与 node trace；adapter 仅保留 `reasoning_summary`，不持久化原始 CoT
> - `plan_actions` / `generate_report`：标准推理（config 可开），同样记录调用元数据
> - prompt 增强：`DIAGNOSIS_PROMPT_TEMPLATE` 显式要求 evidence_id 引用与排序/根因理由
> - 测试：`tests/unit/test_reasoning_layering.py`（17 例）
>
> 待真实环境验证：深度推理节点的实测延迟（本地 GPU smoke / 云端 API ≤10s）需真实模型；未通过真实 provider smoke 前，1.2 只算代码落地。

目标：核心诊断节点启用深度推理（Thinking / Extended Thinking / Reasoner），其余节点走标准推理，兼顾质量与延迟。

不同 LLM 后端的推理深度控制方式：

| 后端 | 深度推理方式 | 控制参数 |
| --- | --- | --- |
| Qwen3（本地） | thinking / reasoning 输出 | vLLM 启动 `--enable-reasoning --reasoning-parser deepseek_r1`，请求侧再传 provider 支持的 thinking 开关 |
| Claude（云端） | Adaptive Thinking | Claude 4.6+ 优先 `thinking={"type":"adaptive"}` + `output_config.effort`；manual `budget_tokens` 仅作旧模型兼容 |
| DeepSeek（云端/本地） | Thinking / Reasoner | 优先新模型的 thinking 参数；`deepseek-reasoner` alias 仅兼容到 2026-07-24 |
| OpenAI（云端） | Reasoning 模型能力 | 按实施时最新 API 使用 reasoning effort / model capability，不只靠模型名切换 |

分层策略（节点对应 `02-agent/langgraph-workflow.md`）：

| 节点 | 推理深度 | 理由 |
| --- | --- | --- |
| parse_alert | 标准 | 结构化提取，无需深度推理 |
| diagnose | **深度推理** | 核心推理节点，需要结构化 rationale（evidence → hypothesis → root cause） |
| rank_hypotheses | 深度推理（可选） | 排序易被幻觉污染，rationale 可验证排序逻辑 |
| plan_actions | 标准 | 动作规划，需平衡风险收益 |
| generate_report | 标准 | 报告生成，结构化输出 |
| context_compression | 标准 | 摘要压缩，低延迟优先 |

**Claude Adaptive Thinking 关键配置**：

```python
response = client.messages.create(
    model="claude-sonnet-4-6",
    thinking={"type": "adaptive"},
    output_config={"effort": "high"},  # diagnose 节点专用
    messages=[...],
)
# response.content 可能包含 thinking 摘要与 text；只持久化可审计 rationale，不默认保存原始 thinking
```

**DeepSeek / OpenAI-compatible Reasoning 关键配置**：

```python
# 新模型优先：通过 thinking / reasoning 参数控制，具体字段以 provider adapter 映射为准
response = client.chat.completions.create(
    model="deepseek-v4-pro",
    messages=[...],
    extra_body={"thinking": {"type": "enabled"}},
)
# 旧兼容：deepseek-reasoner alias 可临时保留，但 2026-07-24 后应移除

# 通用模型不要要求输出原始 CoT；要求结构化 rationale 和 evidence 引用
response = client.chat.completions.create(
    model="qwen3-8b",
    messages=[
        {"role": "system", "content": "Return a concise rationale with evidence IDs, hypotheses, and root cause."},
        ...,
    ],
)
```

**验收标准**：

- diagnose 节点输出包含可审计的结构化 rationale：引用 evidence_id，说明 hypothesis 排序原因和 root cause 选择理由。
- 不把原始 chain-of-thought 作为必须持久化的数据；如 provider 返回 `reasoning_content`，默认只抽取摘要或 rationale。
- 深度推理节点延迟在可接受范围内（本地 GPU 以 smoke eval 实测为准，云端 API ≤10s 为目标）。
- 可通过配置项开关每个节点的深度推理，方便开发调试时关闭以加速迭代。

## 1.3 证据交叉验证

> **实现状态（已落地）**：`packages/agent/evidence_validation.py`（纯函数、确定性、无 LLM）。
> - `derive_signals(state)` 从 metrics/logs/traces/deployment 四源各抽一个方向信号（anomaly/normal/neutral），空源记为 `degraded`
> - `cross_validate(signals)` 信号融合：≥2 个 anomaly 源 → `corroborated` 提升置信度；anomaly 与 healthy 矛盾 → `conflicting` 置 `needs_human_review`；单源 → `single_source` 不加成
> - 证据权重 Trace 1.0 > Metrics 0.8 > Logs 0.6 > Git/deployment 0.4
> - **deployment 非对称**：有 deploy 是 anomaly 关联信号，无 deploy 为中性（不算 healthy 反对票），避免无发布故障被误判为矛盾
> - 缺失降级：空/失败源记入 `degraded_sources`，流程不中断
> - `diagnose` 节点接入：按 `confidence_adjustment` 调整 root_cause 置信度（clamp 0.05-0.99），结果写入 `state["cross_validation"]` 与 `state["needs_human_review"]`（informational，未接入 guardrail）
> - 测试：`tests/unit/test_evidence_validation.py`（23 例）
>
> 说明：时序对齐为最佳努力实现（demo fixture 缺逐条精确时间戳，以源级方向信号 + incident time_window 为准）；真实 Trace/Tempo 接入后（Phase 2）可加逐 span 时间对齐。

目标：指标、日志、Trace 三方印证，减少误判（工具层见 `03-tools/tools.md`）。

| 任务 | 细节 |
| --- | --- |
| 时序对齐 | 将 Prometheus 异常时间点与 Loki 日志、OTel Trace 时间窗口对齐 |
| 信号融合 | 多源信号一致 → 提升置信度；信号矛盾 → 标记为需人工确认 |
| 缺失降级 | 某一数据源为空时，不中断流程，标注 `degraded` 继续 |
| 证据权重 | 不同类型证据赋予不同权重（Trace > Metrics > Logs > Git） |

**验收标准**：多源证据一致的场景置信度 > 单源；证据矛盾时自动标记 `needs_human_review`。

## 1.4 级联故障分析

> **实现状态（已落地）**：`packages/agent/topology.py`（纯函数、确定性）。
> - `ServiceTopology` — 服务依赖图，支持 `from_config()`（配置文件）、`from_trace_spans()`（OTel span 的 `service → downstream_service`）、`from_file()`；提供 `dependencies()` / `dependents()` / `is_adjacent()`
> - 配置文件示例：`demo/topology.json`；setting 新增 `service_topology_path`
> - `analyze_propagation(topology, anomalous)` — 故障传播建模：根服务 = 下游无异常依赖的最底层异常服务，上游调用方标为 cascade，输出 `root_services` / `cascade_services` / `chains` / `is_cascade`；互不依赖的多服务不误判为级联
> - `correlate_incidents(incidents, topology, window)` — 同时间窗 + 拓扑相邻的 incident 用并查集聚类，输出建议提级 severity 与根服务
> - `analyze_cascade_from_state(state)` — 从 incident 自身 trace error span 自建拓扑并分析；接入 `diagnose`，结果写入 `state["cascade_analysis"]`（informational，单服务时 `is_cascade=False`，不改变现有决策）
> - 测试：`tests/unit/test_topology.py`（19 例）
>
> 说明：MVP 为单 demo-service（见 `00-overview/scope.md`），级联分析作为能力模块就绪并以 trace 派生拓扑自洽运行；真正跨服务拓扑与批量关联在多服务真实接入（Phase 2）后发挥全部价值。

目标：从单服务扩展到跨服务依赖链。

| 任务 | 细节 |
| --- | --- |
| 服务拓扑图 | 从 OTel Trace 或配置文件构建 `checkout → payments → postgres` 依赖图 |
| 故障传播建模 | 上游故障 → 下游告警 → 自动标记为级联，聚焦根服务 |
| 批量 Incident 关联 | 同一时间窗口多个 incident → 自动 Cluster + 提级 |
