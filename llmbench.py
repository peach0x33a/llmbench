#!/usr/bin/env python3
"""llmbench — 一个脚本测任意 LLM API 的延迟与吞吐。

合并自 /root 下的 bench_latency.sh、bench_ttft.sh、bench_full.sh、
bench_ipv4_test.py、bench_suite.py、bench-6luna.py。

来源（第一个位置参数）可以是本机已装 Coding Agent 的配置，也可以是裸 URL：

  opencode:<provider>   读 ~/.config/opencode/opencode.jsonc（省略前缀也行，直接写 provider 名）
  claude                读 ~/.claude/settings.json 的 env（Anthropic 协议）
  codex                 读 ~/.codex/config.toml 的 [model_providers.*]
  https://host/v1       裸地址，配合 --key / --key-env

例：
  ./llmbench.py --list
  ./llmbench.py claudeflare --model deepseek-v4-flash --runs 3
  ./llmbench.py opencode:bacon-responses --model gpt-6-luna --context-tokens 200000
  ./llmbench.py claude --model claude-sonnet-5-5 --runs 2
  ./llmbench.py codex --runs 2
  ./llmbench.py https://claudeflare.top/v1 --key-env CF_KEY --model deepseek-v4-flash

指标：
  TTFB    从发出请求到收到响应头
  think   响应头 → 第一个思考 token（网关把思考流式下发时才有，否则为 -）
  TTFT    从发出请求到第一个可见正文 token（隐藏思考会算进来）
  prefill prompt_tokens / TTFT（含思考，是纯 prefill 的下界）
  decode  可见输出 token / (总耗时 - TTFT)
"""

from __future__ import annotations

import argparse
import codecs
import json
import os
import re
import socket
import statistics
import sys
import time
import tomllib
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace

import requests

# 单例 session：便于统一开/关代理（--no-proxy）
SESSION = requests.Session()

OPENCODE_CONFIG = os.path.expanduser("~/.config/opencode/opencode.jsonc")
CLAUDE_CONFIG = os.path.expanduser("~/.claude/settings.json")
CODEX_CONFIG = os.path.expanduser("~/.codex/config.toml")

# Cloudflare 会把 Python 默认 UA 当爬虫拦掉，默认伪装成 curl。
DEFAULT_UA = os.environ.get("LLMBENCH_UA", "curl/8.5.0")

FILLER_SENTENCE = (
    "The quick brown fox jumps over the lazy dog while distant thunder rolls "
    "across the valley and the river keeps moving north past the old mill."
)
# 这条 filler 实测约 5.0 字符/token，乘 5.2 让实际上下文略超目标值，
# 真实 token 数一律以 usage 为准。
CHARS_PER_TOKEN = 5.2

DEFAULT_PROMPT = (
    "用大约 400 字说明大模型推理的 prefill 和 decode 两个阶段，"
    "以及为什么测速要看 TTFT 和 tok/s，最后给一个简短的对比表。"
)

PROTOCOLS = ("openai", "responses", "anthropic")

# ---------------------------------------------------------------- 来源解析


@dataclass
class Source:
    name: str
    base: str
    key: str = ""
    protocol: str = "openai"
    models: list = field(default_factory=list)
    default_model: str | None = None
    variants: dict = field(default_factory=dict)
    origin: str = ""
    extra_headers: dict = field(default_factory=dict)

    def endpoint(self) -> str:
        b = self.base.rstrip("/")
        if self.protocol == "openai":
            return b if b.endswith("/chat/completions") else b + "/chat/completions"
        if self.protocol == "responses":
            return b if b.endswith("/responses") else b + "/responses"
        if b.endswith("/messages"):
            return b
        return b + "/messages" if b.endswith("/v1") else b + "/v1/messages"

    def headers(self) -> dict:
        h = {"Content-Type": "application/json", "User-Agent": DEFAULT_UA}
        if self.protocol == "anthropic":
            # Claude Code 用 ANTHROPIC_AUTH_TOKEN 时发的是 Bearer；
            # 兼容网关两种都收，所以一起发。
            h["x-api-key"] = self.key
            h["anthropic-version"] = "2023-06-01"
            h["Authorization"] = f"Bearer {self.key}"
        else:
            h["Authorization"] = f"Bearer {self.key}"
        h.update(self.extra_headers)
        return h


def strip_jsonc(text: str) -> str:
    """去掉 // 与 /* */ 注释和尾逗号。字符串里的内容原样保留。"""
    out = []
    i, n = 0, len(text)
    in_str = esc = False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(c)
        i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def protocol_from_package(pkg: str) -> str:
    pkg = pkg or ""
    if "responses" in pkg:
        return "responses"
    if "anthropic" in pkg:
        return "anthropic"
    return "openai"


def load_opencode(path: str = OPENCODE_CONFIG) -> dict[str, Source]:
    with open(path, encoding="utf-8") as fh:
        cfg = json.loads(strip_jsonc(fh.read()))
    out: dict[str, Source] = {}
    for pid, prov in (cfg.get("providers") or {}).items():
        prov = prov or {}
        settings = prov.get("settings") or {}
        base = settings.get("baseURL") or settings.get("base_url") or ""
        if not base:
            continue
        proto = protocol_from_package(prov.get("package"))
        models, variants = [], {}
        for mname, meta in (prov.get("models") or {}).items():
            meta = meta or {}
            if meta.get("disabled"):
                continue
            models.append(mname)
            if meta.get("variants"):
                variants[mname] = meta["variants"]
        out[pid] = Source(
            name=pid,
            base=base,
            key=settings.get("apiKey") or settings.get("api_key") or "",
            protocol=proto,
            models=models,
            default_model=models[0] if len(models) == 1 else None,
            variants=variants,
            origin=f"opencode {path}",
        )
    return out


def load_claude(path: str = CLAUDE_CONFIG) -> Source | None:
    try:
        with open(path, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    env = cfg.get("env") or {}
    base = env.get("ANTHROPIC_BASE_URL") or "https://api.anthropic.com"
    key = env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY") or ""
    models = []
    for k in (
        "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    ):
        v = env.get(k)
        if v:
            models.append(re.sub(r"\[[^\]]*\]$", "", v))
    pick = cfg.get("model") or "sonnet"
    default = None
    for alias, k in (
        ("sonnet", "ANTHROPIC_DEFAULT_SONNET_MODEL"),
        ("opus", "ANTHROPIC_DEFAULT_OPUS_MODEL"),
        ("haiku", "ANTHROPIC_DEFAULT_HAIKU_MODEL"),
    ):
        if pick == alias and env.get(k):
            default = re.sub(r"\[[^\]]*\]$", "", env[k])
    return Source(
        name="claude",
        base=base,
        key=key,
        protocol="anthropic",
        models=models,
        default_model=default or (models[0] if models else None),
        origin=f"claude {path}",
    )


def load_codex(path: str = CODEX_CONFIG) -> Source | None:
    try:
        with open(path, "rb") as fh:
            cfg = tomllib.load(fh)
    except (FileNotFoundError, tomllib.TOMLDecodeError):
        return None
    providers = cfg.get("model_providers") or {}
    name = cfg.get("model_provider")
    prov = providers.get(name) if name else None
    if prov is None:
        for pname, p in providers.items():
            if p.get("base_url"):
                name, prov = pname, p
                break
    if not prov or not prov.get("base_url"):
        return None
    key = prov.get("experimental_bearer_token") or ""
    if not key and prov.get("env_key"):
        key = os.environ.get(prov["env_key"], "")
    models = [m for m in (cfg.get("model"), (cfg.get("agents") or {}).get("default_subagent_model")) if m]
    return Source(
        name="codex",
        base=prov["base_url"],
        key=key,
        protocol="responses" if prov.get("wire_api") == "responses" else "openai",
        models=list(dict.fromkeys(models)),
        default_model=cfg.get("model"),
        origin=f"codex {path} ({name})",
    )


def all_sources() -> dict[str, Source]:
    found: dict[str, Source] = {}
    try:
        found.update(load_opencode())
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    for loader, alias in ((load_claude, "claude"), (load_codex, "codex")):
        src = loader()
        if src and src.key:
            found[alias] = src
    return found


def discover(spec: str, args) -> Source:
    if spec.startswith("http://") or spec.startswith("https://"):
        key = args.key or ""
        if not key and args.key_env:
            key = os.environ.get(args.key_env, "")
        if not key:
            raise SystemExit("裸 URL 需要 --key 或 --key-env 提供凭据")
        proto = args.protocol or (
            "responses" if spec.rstrip("/").endswith("/responses")
            else "anthropic" if "/messages" in spec
            else "openai"
        )
        return Source(name=spec, base=spec, key=key, protocol=proto, origin="--url")

    known = all_sources()
    want = spec.split(":", 1)[1] if spec.startswith("opencode:") else spec
    if want in known:
        src = known[want]
        if not src.key:
            raise SystemExit(f"来源 {want} 没有可用 apiKey")
        if args.protocol:
            src.protocol = args.protocol
        return src
    raise SystemExit(
        f"认不出来源 {spec!r}。可用：{', '.join(sorted(known))}"
        f"（裸地址请写成 https://... 并配 --key-env）\n用 --list 看详情"
    )


# ---------------------------------------------------------------- 请求构造


@dataclass
class RunCfg:
    model: str
    prompt: str
    max_tokens: int = 1200
    stream: bool = True
    effort: str | None = None
    variant: dict | None = None
    temperature: float = 0.0
    timeout: float = 300.0
    retries: int = 1


def build_payload(src: Source, cfg: RunCfg) -> dict:
    effort = cfg.effort
    if cfg.variant:
        effort = (cfg.variant.get("settings") or {}).get("reasoningEffort", effort)

    if src.protocol == "openai":
        p = {
            "model": cfg.model,
            "messages": [{"role": "user", "content": cfg.prompt}],
            "stream": cfg.stream,
            "temperature": cfg.temperature,
            "max_tokens": cfg.max_tokens,
        }
        if cfg.stream:
            p["stream_options"] = {"include_usage": True}
        if effort and effort != "none":
            p["reasoning_effort"] = effort
    elif src.protocol == "responses":
        p = {
            "model": cfg.model,
            "input": [{"role": "user", "content": cfg.prompt}],
            "stream": cfg.stream,
            "max_output_tokens": cfg.max_tokens,
            "temperature": cfg.temperature,
        }
        if effort and effort != "none":
            p["reasoning"] = {"effort": "high" if effort == "max" else effort}
    else:  # anthropic
        p = {
            "model": cfg.model,
            "max_tokens": cfg.max_tokens,
            "messages": [{"role": "user", "content": cfg.prompt}],
            "stream": cfg.stream,
            "temperature": cfg.temperature,
        }
        if effort and effort != "none":
            budget = {"low": 1024, "high": 4096, "max": 8192}.get(effort, 2048)
            p["thinking"] = {"type": "enabled", "budget_tokens": max(1024, min(budget, cfg.max_tokens - 1))}

    if cfg.variant and (cfg.variant.get("body") or {}):
        # opencode 变体的 body 是原样覆盖（例如 {"thinking": {"type": "disabled"}}）
        p.update(cfg.variant["body"])
    return p


# ---------------------------------------------------------------- 响应解析


def norm_usage(proto: str, usage: dict) -> dict:
    """把三种协议的 usage 归一成 prompt/completion/reasoning/cached。"""
    if not usage:
        return {}
    if proto == "anthropic":
        return {
            "prompt_tokens": usage.get("input_tokens"),
            "completion_tokens": usage.get("output_tokens"),
            "reasoning_tokens": (usage.get("output_tokens_details") or {}).get("thinking_tokens"),
            "cached_tokens": usage.get("cache_read_input_tokens"),
        }
    if proto == "responses":
        return {
            "prompt_tokens": usage.get("input_tokens"),
            "completion_tokens": usage.get("output_tokens"),
            "reasoning_tokens": (usage.get("output_tokens_details") or {}).get("reasoning_tokens"),
            "cached_tokens": (usage.get("input_tokens_details") or {}).get("cached_tokens"),
        }
    return {
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "reasoning_tokens": (usage.get("completion_tokens_details") or {}).get("reasoning_tokens"),
        "cached_tokens": (usage.get("prompt_tokens_details") or {}).get("cached_tokens"),
    }


def estimate_tokens(text: str) -> int:
    """中日韩字符约 0.7 token/字，其余约 3.8 字符/token。只在网关数字不可信时兜底。"""
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff" or "\u3040" <= ch <= "\u30ff")
    return est_from_counts(len(text), cjk)


def est_from_counts(chars: int, cjk: int) -> int:
    return max(1, round(cjk * 0.7 + (chars - cjk) / 3.8))


def usage_is_sane(reported: int | None, chars: int, cjk: int) -> bool:
    """回传的 completion_tokens 和实际字符量差一个数量级时判为不可信。"""
    if not reported:
        return False
    if chars <= 0:
        # 没有可见正文（全是思考）时没有可对照的量，信任 usage
        return True
    est = est_from_counts(chars, cjk)
    return 0.25 <= reported / max(1, est) <= 4.0


def prompt_tokens_of(u: dict, cfg: "RunCfg") -> tuple[int, bool]:
    """网关回传的 input_tokens 不可信时（Claude 经 new-api 中转常见），按字符估。"""
    reported = u.get("prompt_tokens") or 0
    est = estimate_tokens(cfg.prompt)
    if reported and reported >= est / 3:
        return reported, False
    return est, True


def run_once(src: Source, cfg: RunCfg) -> dict:
    payload = build_payload(src, cfg)
    res = {"ok": False, "error": None, "status": None, "stream": cfg.stream}
    t0 = time.perf_counter()
    try:
        resp = SESSION.post(
            src.endpoint(),
            json=payload,
            headers=src.headers(),
            stream=cfg.stream,
            timeout=cfg.timeout,
        )
    except Exception as exc:  # noqa: BLE001
        res.update(error=f"{type(exc).__name__}: {exc}", total=time.perf_counter() - t0)
        return res

    try:
        ttfb = time.perf_counter() - t0
        res.update(ttfb=ttfb, status=resp.status_code)
        if resp.status_code >= 400:
            body = resp.text[:400].replace("\n", " ")
            res.update(error=f"HTTP {resp.status_code}: {body}", total=time.perf_counter() - t0)
            return res

        if not cfg.stream:
            body = resp.json()
            total = time.perf_counter() - t0
            text, think = "", ""
            if src.protocol == "anthropic":
                for block in body.get("content") or []:
                    if block.get("type") == "text":
                        text += block.get("text") or ""
                    elif block.get("type") == "thinking":
                        think += block.get("thinking") or ""
                usage = body.get("usage") or {}
            elif src.protocol == "responses":
                for item in body.get("output") or []:
                    for part in item.get("content") or []:
                        if part.get("type") == "output_text":
                            text += part.get("text") or ""
                        elif part.get("type") == "reasoning_text":
                            think += part.get("text") or ""
                usage = body.get("usage") or {}
            else:
                msg = ((body.get("choices") or [{}])[0]).get("message") or {}
                text = msg.get("content") or ""
                think = msg.get("reasoning_content") or msg.get("reasoning") or ""
                usage = body.get("usage") or {}
            u = norm_usage(src.protocol, usage)
            reported = u.get("completion_tokens")
            tcjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
            if usage_is_sane(reported, len(text), tcjk):
                ct = reported
                usage_estimated = False
            else:
                ct = estimate_tokens(text)
                usage_estimated = True
            pt, estimated = prompt_tokens_of(u, cfg)
            res.update(
                ok=True,
                total=total,
                ttft=None,
                think=None,
                model_returned=body.get("model"),
                prompt_tokens=pt,
                prompt_tokens_estimated=estimated,
                completion_tokens=ct,
                usage_estimated=usage_estimated,
                reasoning_tokens=u.get("reasoning_tokens"),
                content_tokens=max(0, ct - (u.get("reasoning_tokens") or 0)),
                cached_tokens=u.get("cached_tokens"),
                chars=len(text),
                content=text[:4000],
                reasoning=think[:2000],
            )
            r = res
            r["decode"] = (r["content_tokens"] / total) if (total > 0 and r["content_tokens"]) else None
            r["prefill"] = None
            return r

        chars = rchars = cjk = content_events = 0
        content_parts: list[str] = []
        think_parts: list[str] = []
        t_any = t_think = t_content = None
        usage: dict = {}
        event = None
        buf = ""
        model_returned = None
        done = False
        # 逐字节读保证 TTFT 精度，但不能逐字节 decode，否则多字节中文会被打碎
        decoder = codecs.getincrementaldecoder("utf-8")("replace")

        for chunk in resp.iter_content(chunk_size=1):
            buf += decoder.decode(chunk)
            while "\n" in buf and not done:
                line, buf = buf.split("\n", 1)
                line = line.rstrip("\r")
                if not line:
                    continue
                if line.startswith("event:"):
                    event = line[6:].strip()
                    continue
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                event = None
                if data == "[DONE]":
                    done = True
                    break
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue

                c = r = ""
                if src.protocol == "openai":
                    for choice in obj.get("choices") or []:
                        delta = choice.get("delta") or {}
                        piece = delta.get("content")
                        if isinstance(piece, list):
                            c += "".join(p.get("text", "") for p in piece if isinstance(p, dict))
                        elif piece:
                            c += piece
                        r += delta.get("reasoning_content") or delta.get("reasoning") or ""
                    if obj.get("usage"):
                        usage = obj["usage"]
                    model_returned = model_returned or obj.get("model")
                elif src.protocol == "responses":
                    typ = obj.get("type") or event
                    if typ == "response.output_text.delta":
                        c = obj.get("delta") or ""
                    elif typ in ("response.reasoning_summary_text.delta", "response.reasoning_text.delta"):
                        r = obj.get("delta") or ""
                    elif typ == "response.completed":
                        resp_obj = obj.get("response") or {}
                        usage = resp_obj.get("usage") or {}
                        model_returned = model_returned or resp_obj.get("model")
                else:
                    typ = obj.get("type")
                    if typ == "content_block_delta":
                        d = obj.get("delta") or {}
                        if d.get("type") == "text_delta":
                            c = d.get("text") or ""
                        elif d.get("type") == "thinking_delta":
                            r = d.get("thinking") or ""
                    elif typ == "message_start":
                        msg = obj.get("message") or {}
                        usage = dict(msg.get("usage") or {})
                        model_returned = model_returned or msg.get("model")
                    elif typ == "message_delta":
                        usage = {**usage, **(obj.get("usage") or {})}

                if r:
                    now = time.perf_counter() - t0
                    if t_any is None:
                        t_any = now
                    if t_think is None:
                        t_think = now
                    rchars += len(r)
                    if len(think_parts) < 400:
                        think_parts.append(r)
                if c:
                    now = time.perf_counter() - t0
                    if t_any is None:
                        t_any = now
                    if t_content is None:
                        t_content = now
                    chars += len(c)
                    content_events += 1
                    cjk += sum(1 for ch in c if "\u4e00" <= ch <= "\u9fff" or "\u3040" <= ch <= "\u30ff")
                    if len(content_parts) < 400:
                        content_parts.append(c)

        total = time.perf_counter() - t0
        resp.close()
        u = norm_usage(src.protocol, usage)
        rt = u.get("reasoning_tokens")
        reported = u.get("completion_tokens")
        if usage_is_sane(reported, chars, cjk):
            content_tokens = max(0, reported - (rt or 0))
            ct = reported
            usage_estimated = False
        else:
            # usage 缺失或明显不合理（new-api 中转常只回最后一小块）时按字符估
            content_tokens = est_from_counts(chars, cjk)
            ct = content_tokens + (rt or 0)
            usage_estimated = bool(chars)
        pt, estimated = prompt_tokens_of(u, cfg)
        # TTFT 严格指第一个可见正文 token；只有思考没有正文时退回首个 token
        ttft = t_content if t_content is not None else t_any
        # 整段响应被网关缓冲后一次性吐出时，decode 测不出来
        buffered = content_events < 3 and content_tokens > 20
        res.update(
            ok=True,
            total=total,
            ttfb=ttfb,
            ttft=ttft,
            think=(t_think - ttfb) if t_think else None,
            think_start=t_think,
            model_returned=model_returned,
            prompt_tokens=pt,
            prompt_tokens_estimated=estimated,
            completion_tokens=ct,
            reasoning_tokens=rt,
            content_tokens=content_tokens,
            cached_tokens=u.get("cached_tokens"),
            chars=chars,
            cjk_chars=cjk,
            content_events=content_events,
            buffered=buffered,
            usage_estimated=usage_estimated,
            content="".join(content_parts)[:4000],
            reasoning="".join(think_parts)[:2000],
        )
        res["decode"] = (
            (content_tokens - 1) / (total - ttft)
            if ttft and content_tokens > 1 and total > ttft and not buffered
            else None
        )
        res["prefill"] = pt / ttft if ttft and pt else None
        return res
    except Exception as exc:  # noqa: BLE001
        res.update(error=f"{type(exc).__name__}: {exc}", total=time.perf_counter() - t0)
        return res
    finally:
        try:
            resp.close()
        except Exception:  # noqa: BLE001
            pass


TRANSIENT = (
    "Timeout",
    "Connection",
    "URLError",
    "RemoteDisconnected",
    "UNEXPECTED_EOF",
    "BrokenPipe",
    "reset by peer",
    "ChunkedEncodingError",
    "premature",
    "ProtocolError",
)


def is_transient(err: str | None) -> bool:
    """网络抖动、429 限流、5xx 都值得再试一次；4xx 参数错误不重试。"""
    if not err:
        return False
    if any(m in err for m in TRANSIENT):
        return True
    m = re.match(r"HTTP (\d{3})", err)
    return bool(m) and (m.group(1) == "429" or m.group(1).startswith("5"))


def run_retry(src: Source, cfg: RunCfg) -> dict:
    attempts = 0
    out = {}
    for i in range(cfg.retries + 1):
        attempts += 1
        out = run_once(src, cfg)
        # 端点猜错会 404：openai 与 responses 之间自动换一次，并记住结果
        if not out.get("ok") and str(out.get("error", "")).startswith("HTTP 404"):
            alt = {"openai": "responses", "responses": "openai"}.get(src.protocol)
            if alt:
                trial = run_once(replace(src, protocol=alt), cfg)
                if trial.get("ok"):
                    src.protocol = alt
                    out = trial
                    break
        if out.get("ok") or not is_transient(out.get("error")):
            break
        if i < cfg.retries:
            time.sleep(min(4.0, 2.0 ** i))
    out["attempts"] = attempts
    return out


# ---------------------------------------------------------------- 报告


def stats(values) -> dict | None:
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    n = len(vals)
    return {
        "n": n,
        "min": vals[0],
        "p50": statistics.median(vals),
        "mean": statistics.fmean(vals),
        "p95": vals[min(n - 1, int(round(0.95 * (n - 1))))],
        "max": vals[-1],
    }


def fmt_unit(v, unit: str = "s", nd: int | None = None) -> str:
    if v is None:
        return "-"
    if nd is None:
        nd = 3 if unit == "s" else 1
    return f"{v:.{nd}f}{unit}"


def mask(key: str) -> str:
    if not key:
        return "(无)"
    return f"已配置 {key[:3]}… 长度 {len(key)}"


def list_sources() -> None:
    found = all_sources()
    print("本机可用来源\n")
    for name, src in found.items():
        print(f"  {name}  [{src.protocol}]  {src.base}")
        print(f"    来源文件  {src.origin}")
        print(f"    key       {mask(src.key)}")
        if src.models:
            print(f"    模型      {', '.join(src.models)}")
        if src.variants:
            for m, vs in src.variants.items():
                print(f"    变体      {m}: {', '.join(v.get('id', '?') for v in vs)}")
        print()
    print("裸地址：./llmbench.py https://host/v1 --key-env ENV_NAME --model M")
    print("gemini / antigravity-cli 只有 OAuth token，没有可直测的 API key，未纳入。")


def print_header(src: Source, cfg: RunCfg, args, model) -> None:
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or "直连"
    print(f"来源   {src.name}  [{src.protocol}]  ({src.origin})")
    print(f"地址   {src.base}")
    print(f"模型   {cfg.model}" + ("  ← 来源默认" if model == src.default_model else ""))
    print(
        f"参数   runs={args.runs} conc={args.concurrency} max_tokens={cfg.max_tokens} "
        f"effort={cfg.effort or '-'} ctx={args.context_tokens} 流式={'是' if cfg.stream else '否'}"
    )
    print(f"网络   代理={proxy}  超时={args.timeout}s  重试={cfg.retries}")
    if args.variant:
        print(f"变体   {args.variant}")
    print(f"提示   {cfg.prompt[:80]}{'…' if len(cfg.prompt) > 80 else ''}\n")


def print_round(i: int, r: dict, conc: int) -> None:
    tag = f"#{i}" if conc == 1 else f"#{i}"
    if not r.get("ok"):
        print(f"  {tag}  失败  {r.get('error') or '未知错误'}  (第 {r.get('attempts', 1)} 次尝试)")
        return
    bits = [f"HTTP {r.get('status')}"]
    if r.get("model_returned"):
        bits.append(str(r["model_returned"]))
    bits.append(f"TTFB {fmt_unit(r.get('ttfb'))}")
    if r.get("think") is not None:
        bits.append(f"think {fmt_unit(r.get('think'))}")
    bits.append(f"TTFT {fmt_unit(r.get('ttft'))}")
    bits.append(f"总 {fmt_unit(r.get('total'))}")
    bits.append(f"out {r.get('content_tokens') or 0}tok")
    if r.get("reasoning_tokens"):
        bits.append(f"rsn {r['reasoning_tokens']}tok")
    if r.get("cached_tokens"):
        bits.append(f"cache {r['cached_tokens']}tok")
    if r.get("buffered"):
        bits.append("buffered")
    bits.append(f"decode {fmt_unit(r.get('decode'), ' tok/s')}")
    print(f"  {tag}  " + "  ".join(bits))


def print_stats(ok: list[dict]) -> None:
    rows = [
        ("TTFB", "ttfb", "s"),
        ("think", "think", "s"),
        ("TTFT", "ttft", "s"),
        ("total", "total", "s"),
        ("decode", "decode", " tok/s"),
        ("prefill", "prefill", " tok/s"),
    ]
    print(f"\n  统计 (n={len(ok)})")
    print("  " + f"{'metric':<10}" + "".join(f"{h:>13}" for h in ("min", "p50", "mean", "p95", "max")))
    for label, key, unit in rows:
        s = stats([r.get(key) for r in ok])
        if not s:
            continue
        cells = "".join(f"{fmt_unit(s[k], unit):>13}" for k in ("min", "p50", "mean", "p95", "max"))
        print(f"  {label:<10}{cells}")

    tokens = sum((r.get("content_tokens") or 0) for r in ok)
    reasons = sum((r.get("reasoning_tokens") or 0) for r in ok)
    print(f"\n  可见输出合计 {tokens}tok" + (f"（另隐藏思考 {reasons}tok）" if reasons else ""))
    models = {r.get("model_returned") for r in ok if r.get("model_returned")}
    if models:
        print(f"  网关回显模型 {', '.join(sorted(models))}")
    if any(r.get("prompt_tokens_estimated") for r in ok):
        print("  注：网关未返回可信的 input_tokens，prefill 用的是按字符估算的 prompt 长度")
    if any(r.get("usage_estimated") for r in ok):
        print("  注：部分轮次 usage 的 token 数与实际字符量不符，已按字符估算")
    if any(r.get("buffered") for r in ok):
        print("  注：有轮次响应被网关整段缓冲后一次吐出，decode 测不出来（显示 -）")


def show_preview(ok: list[dict], n: int) -> None:
    if not ok or n <= 0:
        return
    r = ok[0]
    print("\n  实际响应（首轮）")
    print("  " + "-" * 58)
    if r.get("reasoning"):
        print("  [思考] " + r["reasoning"][:n].replace("\n", "\n         "))
    text = (r.get("content") or "").replace("\n", "\n         ")
    print("  [正文] " + (text[:n] if text else "(空)"))
    print("  " + "-" * 58)


# ---------------------------------------------------------------- 主流程


def build_prompt(args) -> str:
    base = args.prompt or DEFAULT_PROMPT
    if args.prompt_file:
        with open(args.prompt_file, encoding="utf-8") as fh:
            base = fh.read()
    if args.context_tokens <= 0:
        return base
    salt = "" if args.fixed_context else f"[session {uuid.uuid4().hex[:8]}]\n"
    target = int(args.context_tokens * CHARS_PER_TOKEN)
    parts, size, i = [], 0, 0
    while size < target:
        i += 1
        line = f"{i}. {FILLER_SENTENCE}\n"
        parts.append(line)
        size += len(line)
    return f"{salt}{''.join(parts)}\n{base}"


def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description="统一 LLM API 测速：TTFB / TTFT / prefill / decode tok/s",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "来源写法：\n"
            "  claudeflare                      opencode 里配置的 provider\n"
            "  opencode:claudeflare             同上，显式\n"
            "  claude | codex                   从对应 CLI 配置读\n"
            "  https://host/v1 --key-env K      裸地址\n"
        ),
    )
    ap.add_argument("source", nargs="?", help="来源：provider 名 / claude / codex / https://...")
    ap.add_argument("--list", action="store_true", help="列出本机可用来源")
    ap.add_argument("--list-models", action="store_true", help="列出模型（优先请求 /models）")
    ap.add_argument("-m", "--model", help="模型名")
    ap.add_argument("--runs", type=int, default=3, help="轮数（默认 3）")
    ap.add_argument("-c", "--concurrency", type=int, default=1, help="每轮并发数（默认 1）")
    ap.add_argument("--max-tokens", type=int, default=1200, help="输出上限（默认 1200）")
    ap.add_argument("--effort", choices=["none", "low", "high", "max"], help="推理力度")
    ap.add_argument("--variant", help="opencode 变体 id（如 none/low/high/max）")
    ap.add_argument("--prompt", help="提示词")
    ap.add_argument("--prompt-file", help="从文件读提示词")
    ap.add_argument("--context-tokens", type=int, default=0, help="前填充上下文 token 数")
    ap.add_argument("--fixed-context", action="store_true", help="固定前缀，便于命中缓存")
    ap.add_argument("--no-stream", action="store_true", help="非流式（只测总耗时）")
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument("--timeout", type=float, default=300.0, help="请求超时秒数（默认 300）")
    ap.add_argument("--retries", type=int, default=1, help="瞬时错误重试次数（默认 1）")
    ap.add_argument("--warmup", type=int, default=1, help="预热请求数，不计入统计（默认 1）")
    ap.add_argument("--interval", type=float, default=0.0, help="轮间隔秒数")
    ap.add_argument("--preview", type=int, default=160, help="打印正文前 N 字，0 关闭")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--key", help="裸 URL 模式的 api key")
    ap.add_argument("--key-env", help="裸 URL 模式：从该环境变量读 api key")
    ap.add_argument("--protocol", choices=PROTOCOLS, help="裸 URL 模式下手动指定协议")
    ap.add_argument("--header", action="append", default=[], metavar="K:V", help="额外请求头，可重复")
    ap.add_argument("--ipv4", action="store_true", help="只解析 IPv4（本机到 CF 的 IPv6 曾间歇丢包）")
    ap.add_argument("--no-proxy", action="store_true", help="忽略 HTTP(S)_PROXY 环境变量")
    ap.add_argument("--user-agent", help="覆盖 User-Agent")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    global DEFAULT_UA
    args = parse_args(argv)

    if args.list:
        list_sources()
        return 0
    if not args.source:
        raise SystemExit("缺少来源。用 --list 看可用来源，或写 ./llmbench.py https://host/v1 --key-env K")

    if args.ipv4:
        orig = socket.getaddrinfo

        def ipv4_only(*a, **kw):
            res = orig(*a, **kw)
            return [r for r in res if r[0] == socket.AF_INET] or res

        socket.getaddrinfo = ipv4_only
    if args.no_proxy:
        SESSION.trust_env = False
    if args.user_agent:
        DEFAULT_UA = args.user_agent

    src = discover(args.source, args)
    for h in args.header:
        if ":" in h:
            k, v = h.split(":", 1)
            src.extra_headers[k.strip()] = v.strip()

    if args.list_models:
        names = src.models
        try:
            r = SESSION.get(src.base.rstrip("/") + "/models", headers=src.headers(), timeout=30)
            if r.status_code == 200:
                names = [m.get("id") for m in (r.json().get("data") or []) if m.get("id")]
        except Exception:  # noqa: BLE001
            pass
        print(f"{src.name}: {', '.join(names) if names else '(无)'}")
        return 0

    model = args.model or src.default_model
    if not model:
        if len(src.models) == 1:
            model = src.models[0]
        else:
            raise SystemExit(f"需要 --model，可选：{', '.join(src.models) or '(来源未配置模型)'}")

    variant = None
    if args.variant:
        cands = src.variants.get(model) or []
        variant = next((v for v in cands if v.get("id") == args.variant), None)
        if variant is None:
            raise SystemExit(f"模型 {model} 没有变体 {args.variant}，可选：{[v.get('id') for v in cands] or '(无)'}")

    cfg = RunCfg(
        model=model,
        prompt=build_prompt(args),
        max_tokens=args.max_tokens,
        stream=not args.no_stream,
        effort=args.effort,
        variant=variant,
        temperature=args.temperature,
        timeout=args.timeout,
        retries=args.retries,
    )

    if not args.json:
        print_header(src, cfg, args, model)

    for w in range(args.warmup):
        warm = RunCfg(**{**cfg.__dict__, "max_tokens": min(cfg.max_tokens, 16)})
        r = run_retry(src, warm)
        if not args.json:
            print(f"  预热 {w + 1}/{args.warmup}  " + ("ok" if r.get("ok") else f"失败: {r.get('error')}"))
    if args.warmup and not args.json:
        print()

    results: list[dict] = []
    wall_start = time.perf_counter()
    for i in range(args.runs):
        if args.concurrency == 1:
            r = run_retry(src, cfg)
            results.append(r)
            if not args.json:
                print_round(i + 1, r, 1)
        else:
            with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
                batch = list(pool.map(lambda _: run_retry(src, cfg), range(args.concurrency)))
            results.extend(batch)
            if not args.json:
                print(f"  -- 第 {i + 1} 轮 并发 {args.concurrency} --")
                for j, r in enumerate(batch):
                    print_round((i * args.concurrency) + j + 1, r, args.concurrency)
        if args.interval and i < args.runs - 1:
            time.sleep(args.interval)
    wall = time.perf_counter() - wall_start

    ok = [r for r in results if r.get("ok")]
    failed = len(results) - len(ok)

    if args.json:
        print(json.dumps(
            {
                "source": src.name,
                "protocol": src.protocol,
                "base": src.base,
                "model": model,
                "variant": args.variant,
                "effort": cfg.effort,
                "context_tokens": args.context_tokens,
                "concurrency": args.concurrency,
                "wall_s": wall,
                "results": results,
                "stats": {
                    k: stats([r.get(k) for r in ok])
                    for k in ("ttfb", "ttft", "think", "total", "decode", "prefill")
                },
            },
            ensure_ascii=False,
            indent=2,
        ))
        return 0 if ok and not failed else 1

    if not ok:
        print("\n  全部失败。检查来源凭据、模型名和网络（--ipv4 / --no-proxy 可排除解析与代理问题）。")
        return 1

    print_stats(ok)
    show_preview(ok, args.preview)

    if args.concurrency > 1:
        tokens = sum((r.get("content_tokens") or 0) for r in ok)
        print(f"\n  并发 {args.concurrency} × {args.runs} 轮：墙钟 {wall:.1f}s，"
              f"聚合可见吞吐 {tokens / wall:.1f} tok/s（含排队等待）")
    if failed:
        print(f"\n  失败 {failed}/{len(results)} 次")
    print()
    return 0 if not failed else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n中断")
        sys.exit(130)
