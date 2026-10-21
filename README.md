# llmbench

量一下某个 LLM API 到底有多快。

一条命令跑完 TTFB、TTFT、prefill 速率和 decode 吞吐。来源可以直接从本机已装的 Coding Agent 里读，不用手抄 baseURL 和 key。单文件，只依赖 `requests`。

## 为什么不用现成的

现成的压测工具大多假设你手上有 endpoint 和 key，跑完只给一个总耗时。真正排查问题时想知道的是另一组事：

- 慢在排队、prefill，还是逐 token 输出
- 思考模型那几秒，算在哪一项里
- 并发压上来以后，吞吐掉多少
- 网关报的 token 数，能不能信

`llmbench` 就按这四个问题来设计：TTFT 只算第一个**可见正文** token，思考过程单独计一列；`--context-tokens` 把 prefill 负载拉起来；`-c` 测并发；usage 和实际字符量对不上时按字符估算，并在输出里注明。

## 安装

```bash
git clone https://github.com/peach0x33a/llmbench.git
cd llmbench
python3 -m pip install requests
./llmbench.py --list
```

需要 Python 3.11+（用到了 `tomllib`）。

## 快速开始

先看本机有哪些来源：

```bash
./llmbench.py --list
```

```
本机可用来源

  claudeflare  [openai]  https://claudeflare.top/v1
    来源文件  opencode /root/.config/opencode/opencode.jsonc
    key       已配置 sk-… 长度 51
    模型      deepseek-v4-flash
    变体      deepseek-v4-flash: none, low, high, max

  claude  [anthropic]  https://eo.ioll.pp.ua
    来源文件  claude /root/.claude/settings.json
    模型      claude-sonnet-5-5, claude-opus-5-5, claude-haiku-5-5
```

然后测一个：

```bash
./llmbench.py claudeflare --runs 3 --max-tokens 2000
```

```
来源   claudeflare  [openai]  (opencode /root/.config/opencode/opencode.jsonc)
地址   https://claudeflare.top/v1
模型   deepseek-v4-flash  ← 来源默认
参数   runs=3 conc=1 max_tokens=2000 effort=- ctx=0 流式=是
网络   代理=http://127.0.0.1:7788  超时=300.0s  重试=1
提示   用大约 400 字说明大模型推理的 prefill 和 decode 两个阶段…

  预热 1/1  ok

  #1  HTTP 200  deepseek-flash  TTFB 1.980s  think 0.062s  TTFT 5.299s  总 6.603s  out 294tok  rsn 553tok  decode 224.6 tok/s
  #2  HTTP 200  deepseek-flash  TTFB 1.405s  think 0.075s  TTFT 3.027s  总 4.632s  out 341tok  rsn 202tok  decode 211.8 tok/s
  #3  HTTP 200  deepseek-flash  TTFB 1.480s  think 0.084s  TTFT 2.684s  总 5.156s  out 385tok  rsn 193tok  decode 155.3 tok/s

  统计 (n=3)
  metric              min          p50         mean          p95          max
  TTFB             1.405s       1.480s       1.622s       1.980s       1.980s
  think            0.062s       0.075s       0.074s       0.084s       0.084s
  TTFT             2.684s       3.027s       3.670s       5.299s       5.299s
  total            4.632s       5.156s       5.464s       6.603s       6.603s
  decode      155.3 tok/s  211.8 tok/s  197.2 tok/s  224.6 tok/s  224.6 tok/s
  prefill      13.2 tok/s   23.1 tok/s   20.8 tok/s   26.1 tok/s   26.1 tok/s

  可见输出合计 1020tok（另隐藏思考 948tok）
  网关回显模型 deepseek-flash

  实际响应（首轮）
  ----------------------------------------------------------
  [思考] We need answer Chinese ~400字 explain prefill/decode, why measure TTFT and tok/s…
  [正文] 大模型推理通常分两阶段。**Prefill**：把整段 prompt 一次性送入模型，并行计算注意力，建立 KV Cache，并产出首个输出 token…
  ----------------------------------------------------------
```

这一跑能直接读出的东西：

- TTFT 2.7–5.3s，TTFB 只有 1.4–2.0s，中间近 3 秒是模型在思考（`rsn 553tok`）——只看总耗时不会知道这段花在哪。
- decode 155–225 tok/s，是正文输出速度，不含思考。
- `out` 是可见 token，`rsn` 是隐藏思考 token，两者都在 `completion_tokens` 里。

## 来源

| 写法 | 读什么 | 协议 |
| --- | --- | --- |
| `<provider>` | `~/.config/opencode/opencode.jsonc` 里的 provider | 按 `package` 判断 openai / responses / anthropic |
| `opencode:<provider>` | 同上，显式前缀 | 同上 |
| `claude` | `~/.claude/settings.json` 的 `env` | anthropic |
| `codex` | `~/.codex/config.toml` 的 `[model_providers.*]` | openai，端点 404 时自动换 responses |
| `https://…` | `--key` 或 `--key-env` | 按路径猜，或 `--protocol` 指定 |

opencode 那份配置按 JSONC 解析，注释和尾逗号都能吃；provider 里声明的模型变体（`variants`）可以用 `--variant` 直接选。

key 只在本进程内存里用，不落盘、不回显，`--list` 只打印「已配置」和长度。

### 用裸地址

```bash
export CF_KEY=sk-xxxx
./llmbench.py https://claudeflare.top/v1 --key-env CF_KEY --model deepseek-v4-flash
```

`--key sk-xxxx` 也能直接给，但会留在 shell history 里，`--key-env` 更合适。需要自定义鉴权时用 `--header 'X-Api-Key: ...'`。

## 指标口径

| 指标 | 定义 | 说明 |
| --- | --- | --- |
| `TTFB` | 发出请求 → 收到响应头 | 含 DNS、TCP、TLS、网关排队 |
| `think` | 响应头 → 第一个思考 token | 只有网关把思考流式下发时才有值 |
| `TTFT` | 发出请求 → 第一个可见正文 token | 隐藏的思考会算进这段时间 |
| `prefill` | `prompt_tokens / TTFT` | 含思考时间，是纯 prefill 的下界 |
| `decode` | 可见输出 token `/ (总耗时 - TTFT)` | 不含思考，和 prefill 不是一回事 |
| `total` | 发出请求 → 流结束 | |

思考 token 计入 `completion_tokens` 却不一定流式下发的模型（比如 gpt-6-luna），靠 `usage.completion_tokens_details.reasoning_tokens` 拆开，`out` 只算可见部分。这样 `decode` 不会因为思考被稀释。

## 参数

**选目标**

| 参数 | 作用 |
| --- | --- |
| `-m, --model` | 模型名；来源里只有一个模型时可省略 |
| `--list` | 列出本机可用来源 |
| `--list-models` | 列模型，优先请求服务端的 `/models` |
| `--variant` | opencode 模型变体，如 `none` / `high` |
| `--effort` | `none` / `low` / `high` / `max` |

**造负载**

| 参数 | 作用 |
| --- | --- |
| `--runs N` | 轮数，默认 3 |
| `-c, --concurrency N` | 每轮并发数，默认 1 |
| `--max-tokens N` | 输出上限，默认 1200 |
| `--context-tokens N` | 前置填充上下文，测长上下文 prefill |
| `--fixed-context` | 固定前缀，便于命中 prompt cache |
| `--prompt` / `--prompt-file` | 换提示词 |
| `--temperature` | 默认 0 |
| `--warmup N` | 预热请求数，不计入统计，默认 1 |

**协议与网络**

| 参数 | 作用 |
| --- | --- |
| `--protocol` | 手动指定 `openai` / `responses` / `anthropic` |
| `--key` / `--key-env` | 裸 URL 模式的凭据 |
| `--header 'K: V'` | 额外请求头，可重复 |
| `--timeout` | 单请求超时秒数，默认 300 |
| `--retries` | 瞬时错误重试次数，默认 1 |
| `--ipv4` | 只解析 IPv4 |
| `--no-proxy` | 忽略 `HTTP(S)_PROXY` |
| `--user-agent` | 覆盖 UA，默认伪装成 curl |

**输出**

| 参数 | 作用 |
| --- | --- |
| `--preview N` | 打印正文前 N 字，0 关闭 |
| `--json` | 机器可读输出，含每轮原始数据与统计 |
| `--interval` | 轮间隔秒数 |

几个组合：

```bash
# 长上下文 prefill + 缓存命中对比
./llmbench.py claudeflare --context-tokens 64000 --runs 4 --fixed-context

# 并发压测
./llmbench.py claudeflare -c 4 --runs 2

# 只看某几个变体
./llmbench.py claudeflare --variant none --runs 3
./llmbench.py claudeflare --variant high --runs 3
```

## 数据可信度

网关回的数据不能照抄，脚本做了四件事：

1. **prompt_tokens 校验**：缺失或明显偏小时按中英混排估算，输出末尾打「注」。
2. **completion_tokens 校验**：和实际字符量差一个数量级（new-api 一类中转常只回最后一块）时，改用字符估算。
3. **缓冲检测**：整段响应被网关缓冲后一次吐出时，`decode` 显示 `-`，不编一个虚高的数。
4. **重试**：429、5xx、连接中断退避重试；4xx 参数错误直接失败，不浪费时间。

`--json` 里每轮都带 `usage_estimated`、`prompt_tokens_estimated`、`buffered`、`content_events` 这些标记，方便自己判断数字的成色。

## 协议兼容

| 协议 | 端点 | 鉴权 | 思考字段 |
| --- | --- | --- | --- |
| `openai` | `POST {base}/chat/completions` | `Authorization: Bearer` | `delta.reasoning_content` / `delta.reasoning` |
| `responses` | `POST {base}/responses` | `Authorization: Bearer` | `response.reasoning_summary_text.delta` |
| `anthropic` | `POST {base}/v1/messages` | `x-api-key` + `Authorization` | `content_block_delta` 的 `thinking_delta` |

`--effort` 会按协议翻译：openai 用 `reasoning_effort`，responses 用 `reasoning.effort`，anthropic 用 `thinking.budget_tokens`（low 1024 / high 4096 / max 8192）。opencode 变体里的 `body` 原样覆盖到请求体上。

openai 端点返回 404 时会自动换 responses 再试一次，试通了就记住该来源的协议。

## 注意事项

- **代理会算进 TTFB**。设了 `HTTP(S)_PROXY` 的话，第一跳是到本地代理，测出来的是「本机 → 代理 → 网关」的合计；要看直连用 `--no-proxy`。
- **网关自身会抖**。同一模型相邻两轮差 2–3 秒很常见，跑 3 轮以上看 p50，比看单次有意义。
- **IPv6 未必通**。部分主机到 Cloudflare 的 IPv6 会间歇丢包，表现为莫名卡十几秒，`--ipv4` 可以排除。
- **Python 默认 UA 会被 Cloudflare 拦**，脚本默认伪装成 `curl/8.5.0`，需要时用 `--user-agent` 覆盖。
- `--no-stream` 只能测总耗时，`TTFT`、`decode` 会是 `-`，适合做粗对比。

## 来历

由六个零散脚本合并而来：分别测连接各阶段耗时、curl 流式首字、完整 TTFT 与 tok/s、IPv4 回退、三档提示词套件，以及专门处理隐藏思考 token 的 gpt-6-luna 压测。口径统一之后，现在只剩这一个入口。

## License

MIT
