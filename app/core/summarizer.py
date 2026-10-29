"""Optional OpenAI-compatible LLM summary generator for transcribed text."""

from __future__ import annotations

from typing import Callable, List, Optional

from app.config import LlmConfig
from app.core import http_client
from app.core.cancel import CancelToken
from app.core.errors import CancelledError
from app.core.transcriber import Segment

LogCallback = Callable[[str], None]

PLACEHOLDER_SUMMARY = "> （待填写）"


def _format_as_blockquote(text: str) -> str:
    lines = [line.rstrip() for line in text.strip().splitlines()]
    if not lines:
        return PLACEHOLDER_SUMMARY
    quoted = []
    for line in lines:
        if line.startswith(">"):
            quoted.append(line)
        elif line == "":
            quoted.append(">")
        else:
            quoted.append(f"> {line}")
    return "\n".join(quoted)


def generate_summary(
    segments: List[Segment],
    title: str,
    llm_cfg: LlmConfig,
    proxy: str = "",
    on_log: Optional[LogCallback] = None,
    token: Optional[CancelToken] = None,
) -> str:
    """Call an OpenAI-compatible `/chat/completions` endpoint (supporting HTTP/SOCKS5 proxy) to summarize the transcript."""
    if not llm_cfg.enabled or not llm_cfg.api_key.strip():
        return PLACEHOLDER_SUMMARY

    if not segments:
        return PLACEHOLDER_SUMMARY

    if token:
        token.check()

    full_text = "\n".join(seg.text for seg in segments if seg.text.strip())
    if not full_text.strip():
        return PLACEHOLDER_SUMMARY

    max_chars = 24000
    if len(full_text) > max_chars:
        full_text = full_text[:max_chars] + "\n...(后文省略)"

    endpoint = llm_cfg.base_url.rstrip("/")
    if not endpoint.endswith("/chat/completions"):
        endpoint = f"{endpoint}/chat/completions"

    payload = {
        "model": llm_cfg.model.strip() or "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": llm_cfg.system_prompt.strip()},
            {
                "role": "user",
                "content": f"视频标题：{title}\n\n转录正文：\n{full_text}",
            },
        ],
        "temperature": 0.3,
    }

    if on_log:
        on_log(f"正在调用 OpenAI 兼容接口生成摘要 ({llm_cfg.model})...")

    try:
        body = http_client.post_json(
            url=endpoint,
            payload=payload,
            headers={"Authorization": f"Bearer {llm_cfg.api_key.strip()}"},
            proxy=proxy,
            timeout=llm_cfg.timeout_sec,
        )
        if token:
            token.check()
        choices = body.get("choices") or []
        if choices:
            content = choices[0].get("message", {}).get("content", "").strip()
            if content:
                if on_log:
                    on_log("LLM 摘要生成完成。")
                return _format_as_blockquote(content)
    except CancelledError:
        raise
    except Exception as exc:
        if on_log:
            on_log(f"[摘要跳过] LLM 接口调用失败，已保留默认占位符：{exc}")

    return PLACEHOLDER_SUMMARY


def test_llm_connection(llm_cfg: LlmConfig, proxy: str = "") -> tuple[bool, str]:
    """Send a lightweight ping request to verify the OpenAI-compatible API endpoint and key."""
    if not llm_cfg.api_key.strip():
        return False, "请先填写 API Key。"
    endpoint = llm_cfg.base_url.rstrip("/")
    if not endpoint.endswith("/chat/completions"):
        endpoint = f"{endpoint}/chat/completions"
    payload = {
        "model": llm_cfg.model.strip() or "gpt-4o-mini",
        "messages": [{"role": "user", "content": "请回复 OK"}],
        "max_tokens": 8,
        "temperature": 0.0,
    }
    try:
        body = http_client.post_json(
            url=endpoint,
            payload=payload,
            headers={"Authorization": f"Bearer {llm_cfg.api_key.strip()}"},
            proxy=proxy,
            timeout=min(20, max(5, llm_cfg.timeout_sec)),
        )
        choices = body.get("choices") or []
        if choices:
            reply = choices[0].get("message", {}).get("content", "").strip()
            return True, f"连接成功！模型响应：{reply or 'OK'}"
        return False, f"收到响应但格式异常：{body}"
    except Exception as exc:
        return False, f"连接失败：{exc}"
