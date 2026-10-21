# llmbench

统一的大模型 API 测速脚本：一条命令测出 TTFB、TTFT、prefill 速率与 decode 吞吐，来源可以直接从本机已装的 Coding Agent 配置里读，不用手抄 baseURL 和 key。

单文件、零依赖（只要 `requests`），从 6 个零散脚本合并而来。

## 仓库位置

| 用途 | 地址 |
| --- | --- |
| 本地仓库 | `/data/Project/llmbench` |
| GitHub | `peach0x33a/llmbench`（公开） |
| 入口 | `./llmbench.py` |

## 快速开始

```bash
./llmbench.py --list                      # 本机所有可用来源
./llmbench.py claudeflare --runs 3        # opencode 里配的 provider，直接写名字
./llmbench.py claude --runs 2             # Claude Code 的 settings.json
./llmbench.py codex                       # Codex CLI 的 model_providers
```

来源也可以是裸地址：

```bash
./llmbench.py https://claudeflare.top/v1 \
  --key-env CF_KEY --model deepseek-v4-flash --runs 3
```

## 来源解析

| 写法 | 读取位置 | 协议 |
| --- | --- | --- |
| `<provider>` / `opencode:<provider>` | `~/.config/opencode/opencode.jsonc`（容忍 JSONC 注释与尾逗号，含 `variants`） | 依 `package` 自动判 `openai` / `responses` / `anthropic` |
| `claude` | `~/.claude/settings.json` 的 `env`，模型名自动去掉 `[1m]` 后缀 | `anthropic` |
| `codex` | `~/.codex/config.toml` 的 `[model_providers.*]` | `openai`，端点 404 时自动换 `responses` 重试 |
| `https://…` | `--key-env` 或 `--key` | `--protocol` 指定，缺省按路径猜 |

key 不落盘、不回显，`--list` 只显示「已配置」和长度。

## 指标口径

| 指标 | 定义 |
| --- | --- |
| `TTFB` | 从发出请求到收到响应头 |
| `think` | 响应头 → 第一个思考 token；只有网关把思考流式下发时才有值 |
| `TTFT` | 从发出请求到第一个**可见正文** token，隐藏思考会算进这段时间 |
| `prefill` | `prompt_tokens / TTFT`。含思考时间，是纯 prefill 的下界 |
| `decode` | 可见输出 token `/ (总耗时 - TTFT)`，不含思考，也不是 prefill |
| `total` | 端到端总耗时 |

思考 token 计入 `completion_tokens` 但不流式下发的模型（如 gpt-6-luna），靠 `usage.completion_tokens_details.reasoning_tokens` 拆开，`out` 只算可见部分。

## 常用开关

```bash
--runs N              轮数（默认 3）
-c, --concurrency N   每轮并发数（默认 1），用来测排队与吞吐衰减
--max-tokens N        输出上限（默认 1200）
--effort none|low|high|max   推理力度，按协议翻译成 reasoning_effort / reasoning.effort / thinking.budget_tokens
--variant ID          opencode 模型变体（如 none/low/high/max）
--context-tokens N    前置填充上下文，测长上下文 prefill
--fixed-context       固定前缀，便于命中 prompt cache
--no-stream           非流式，只测总耗时
--json                机器可读输出
--ipv4                只解析 IPv4
--no-proxy            忽略 HTTP(S)_PROXY
--header 'K: V'       额外请求头，可重复
--preview N           打印正文前 N 字（0 关闭）
```

## 数据可信度处理

真实网关经常谎报 usage，脚本会自己校验：

- `prompt_tokens` 缺失或明显偏小时，按中英混排估算并在末尾打「注」。
- `completion_tokens` 和实际字符量差一个数量级（new-api 中转常只回最后一小块）时，改用字符估算。
- 整段响应被网关缓冲后一次吐出时，`decode` 显示 `-` 而不是给出一个虚高的数字。
- 429、5xx、连接中断会退避重试；4xx 参数错误直接失败。

## 已知环境问题

- claudeflare 上游偶发 `500 failed to evaluate membership entitlement`、`503 workspace_billing_scope_unavailable` 和连接中途掐断，重试通常能过。
- `s2a.bacon-llm.cc.cd` 目前对所有路径返回 404，`bacon-llm.cc.cd` 报 `Invalid token`，因此 `codex` 与 `bacon-responses` 这两个来源暂时不可用。
- Claude 中转（`eo.ioll.pp.ua`）会回 `input_tokens=0` 和错误的 `output_tokens`，同时有 50 次/分钟的限流。
- 本机流量走 `127.0.0.1:7788` 代理，测得的 TTFB 含这一跳。

## 安装

```bash
git clone https://github.com/peach0x33a/llmbench.git
cd llmbench
python3 -m pip install requests   # 唯一依赖
./llmbench.py --list
```
