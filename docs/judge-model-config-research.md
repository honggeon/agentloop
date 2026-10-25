# 裁判模型配置文件化调研报告

> 日期：2026-10-26。背景：裁判模型目前只能通过 `.env` 环境变量配置（`MODEL_PROVIDER`/`MODEL_NAME` + 各家 API key），切换模型要改环境、无多 profile 概念、前端不可选。需求：提供"在配置文件中配置大模型"的可选项。

## 1. 现状链路

```
.env (MODEL_PROVIDER / MODEL_NAME / *_API_KEY)
  └─ auditrail/agents/agent.py  ← import 时读取并固化为模块常量 MODEL_SPEC
       ├─ get_agent()           ← create_deep_agent(model=MODEL_SPEC, ...)
       └─ build_subagents(MODEL_SPEC)
  └─ auditrail/judges/deepagent_engine.py  ← 仅 metadata 记录 provider/model
  ├─ CLI: judge-run            ← cli.py._load_dotenv() 在 engine import 前加载 .env
  └─ 后端: routes/judge.py     ← resolve_judge_engine(engine) → engine.run()
```

关键事实：

1. **import 时绑定**：`agent.py:22-24` 在模块导入时读取环境变量生成 `MODEL_SPEC`。要支持配置文件，必须改为运行时解析（`get_agent()` 每次调用时读取），否则配置加载顺序会成为隐式契约（`cli.py:32` 的注释已暴露这个脆弱性）。
2. **引擎已可选，模型不可选**：`GET /api/judge/engines` + 前端已有引擎选择器（deepagent/dsh），模型选择是天然的平行扩展位。
3. **langchain 能力足够**：`init_chat_model("provider:model", **kwargs)`，`kwargs` 直通底层客户端（OpenAI 系的 `base_url`、Anthropic 系的 `base_url` 都支持）；也支持 `configurable_fields` 运行时重绑定。
4. **dsh 引擎的模型走 DSH profile**（`~/.dsh/profiles/*/cordis.patch.yml`），与 deepagent 引擎是两套体系，本次需求只涉及 deepagent 引擎。

## 2. 仓库已有可复用模式

| 模式 | 出处 | 可借鉴点 |
|---|---|---|
| `token_env`：配置里只放环境变量**名**，凭据值不落盘 | `auditrail/integrations/langfuse.py:98-119` | 配置文件不存明文 key，安全红线 |
| DSH profile YAML：`apiKeyEnv` + `baseURL` + 模型清单 | `~/.dsh/profiles/headless/cordis.patch.yml` | 成熟的 provider/model 声明式 schema |
| 引擎注册表 + availability | `auditrail/judges/base.py` | 模型 profile 可用性检查可复用同形态 |
| 测试隔离守卫 `AUDITRAIL_LOAD_DOTENV` | `tests/conftest.py` | 配置文件加载也需要同类守卫 |
| 配置集中读取 | `backend/app/config.py` | 配置文件路径解析、默认值应集中 |

## 3. 方案对比

### 方案 A：多组环境变量（最小改动）

约定前缀分组，如 `JUDGE_MODEL_KIMI__PROVIDER=openai`、`JUDGE_MODEL_KIMI__NAME=kimi-for-coding`，`JUDGE_MODEL=kimi` 选择启用组。

- 优点：无新文件格式、无解析器、改动最小。
- 缺点：组数一多 `.env` 迅速失控；无法表达模型元数据（显示名、上下文窗口）；前端选择器没有可枚举的来源。**不推荐**。

### 方案 B：YAML 配置文件 + profile 选择（推荐）

新增 `config/judge_models.yaml`（路径可用 `AUDITRAIL_JUDGE_MODEL_CONFIG` 覆盖），示例：

```yaml
default: kimi-for-coding
profiles:
  kimi-for-coding:
    provider: openai                # langchain provider
    model: kimi-for-coding
    base_url: https://api.kimi.com/coding/v1   # 可选
    api_key_env: OPENAI_API_KEY     # 只放变量名，凭据不落盘
    # temperature: 0                # 可选，未来调稳定性
  deepseek:
    provider: deepseek
    model: deepseek-v4-flash
    api_key_env: DEEPSEEK_API_KEY
  kimi-k3:
    provider: openai
    model: k3
    base_url: https://api.kimi.com/coding/v1
    api_key_env: OPENAI_API_KEY
```

- `agent.py` 改为运行时解析：`get_agent()` 读取选定 profile → `init_chat_model(f"{provider}:{model}", base_url=...)`，缺 key 时抛明确 `RuntimeError`（沿用现有缺依赖行为）。
- 选择优先级：请求参数（CLI `--model-profile` / API body）> 环境变量 `JUDGE_MODEL_PROFILE` > 配置文件 `default` > 现状 env 兜底（`MODEL_PROVIDER`/`MODEL_NAME`，保持旧行为兼容）。
- 前端在现有引擎选择器旁加模型 profile 下拉（数据源 `GET /api/judge/models`，返回 profile 名 + 可用性，**不下发任何凭据信息**）。
- pyyaml 已在依赖中（6.0.3），无新增依赖。

### 方案 C：复用 DSH profiles

让 deepagent 裁判也读 `~/.dsh/profiles` 的 YAML。缺点：把 core 包耦合到 DSH 私有格式（schema 不受本仓库控制），且违反"确定性路径不依赖 DSH 运行时"的分层。**不推荐**。

## 4. 技术约束与红线

1. **凭据不进配置文件**（`api_key_env` 模式），配置文件本身可提交，`.env` 继续 gitignore。
2. **向后兼容**：无配置文件 / 无选择参数时行为与今天完全一致（env 兜底）。
3. **测试隔离**：配置文件加载需尊重 `AUDITRAIL_LOAD_DOTENV=0` 同类守卫；确定性测试用临时 YAML + `tmp_path`，不读开发者真实配置。
4. **运行时解析取代 import 绑定**：`MODEL_SPEC` 模块常量需保留为"env 兜底"的默认值以兼容现有 import，但实际 model 在 `get_agent()` 内构建。
5. **judge_reliability / metadata**：`deepagent_engine._metadata()` 应记录实际生效的 profile 名与 provider/model，保证报告可溯源。

## 5. 影响面清单

| 层 | 改动 |
|---|---|
| `auditrail/agents/agent.py` | MODEL_SPEC import 绑定 → 运行时解析 + profile 加载函数 |
| `auditrail/judges/deepagent_engine.py` | 接受 profile 参数；metadata 记录生效 profile |
| `auditrail/cli.py` | `judge-run --model-profile` 选项 |
| `backend/app/config.py` | 配置文件路径设置项 |
| `backend/app/routes/judge.py` | `GET /api/judge/models` + judge 触发接受 profile 参数 |
| `frontend/src/api.ts` + `RunView.vue` | 模型 profile 下拉（复用引擎选择器形态） |
| `tests/` | profile 解析、优先级、缺 key 报错、旧 env 兼容、测试隔离 |
| `README.md` / `.env.example` / `config/judge_models.yaml.example` | 文档与样例 |

## 6. 建议

采用**方案 B**，分两期：

- 一期（核心）：YAML 配置 + 运行时解析 + env 兜底 + CLI `--model-profile` + 单测。不改前端。
- 二期（界面）：`GET /api/judge/models` + 前端下拉 + 报告 metadata 展示 profile 名。
