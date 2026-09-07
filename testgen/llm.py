"""
LLM 客户端（OpenAI 兼容 Chat Completions）

只依赖 requests，不引入 openai SDK。职责：
- 发一次对话请求，带重试
- 强制要求返回 JSON，并从回复里稳健地抽出 JSON（容忍 ```json 包裹、前后噪声）
"""

import json
import re
import time
from typing import Any, Optional

import requests

import config


class LLMError(Exception):
    """LLM 调用或解析失败"""


class LLMClient:
    """OpenAI 兼容的最小 Chat 客户端"""

    api_key: str
    base_url: str
    model: str

    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None,
                 model: Optional[str] = None):
        self.api_key = api_key if api_key is not None else config.get_llm_api_key()
        self.base_url = (base_url or config.LLM_BASE_URL).rstrip("/")
        self.model = model or config.LLM_MODEL

        if not self.api_key:
            raise LLMError(
                "未配置 LLM API key：请设置环境变量 LLM_API_KEY 或在 credentials.yaml 里填 llm_api_key"
            )

    def chat_json(self, system: str, user: str) -> dict[str, Any]:
        """
        发一轮对话并要求返回 JSON 对象，解析后返回 dict。

        用 response_format=json_object 提高稳定性；即便如此仍做一次
        兜底抽取，兼容不支持该参数或偶发夹带说明文字的模型。
        """
        content = self._chat(system, user)
        return self._extract_json(content)

    def _chat(self, system: str, user: str) -> str:
        url = f"{self.base_url}/chat/completions"
        request_timeout = (config.LLM_CONNECT_TIMEOUT, config.LLM_TIMEOUT)
        print(
            f"  [LLM] model={self.model} | "
            f"connect timeout={config.LLM_CONNECT_TIMEOUT}s | "
            f"read timeout={config.LLM_TIMEOUT}s"
        )
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        payload = {
            "model": self.model,
            "temperature": config.LLM_TEMPERATURE,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_object"},
        }

        last_error = None
        for attempt in range(config.LLM_MAX_RETRIES + 1):
            try:
                resp = requests.post(
                    url, json=payload, headers=headers, timeout=request_timeout
                )
                # 部分兼容实现不认 response_format，遇 400 去掉重试一次
                if resp.status_code == 400 and "response_format" in payload:
                    payload.pop("response_format", None)
                    continue
                _ = resp.raise_for_status()
                data = resp.json()
                content = data["choices"][0]["message"]["content"]
                return str(content)
            except requests.exceptions.RequestException as e:
                last_error = e
                print(f"  [LLM 重试 {attempt + 1}/{config.LLM_MAX_RETRIES + 1}] {e}")
                if attempt < config.LLM_MAX_RETRIES:
                    time.sleep(2)
            except (KeyError, ValueError) as e:
                raise LLMError(f"LLM 响应结构异常: {e}")

        raise LLMError(f"LLM 请求失败，已重试 {config.LLM_MAX_RETRIES} 次: {last_error}")

    @staticmethod
    def _extract_json(text: str) -> dict[str, Any]:
        """从模型回复里抽出 JSON 对象。"""
        if not text:
            raise LLMError("LLM 返回空内容")

        candidates: list[str] = [text]

        # ```json ... ``` 包裹
        fenced = re.search(r"```(?:json)?\s*(.+?)```", text, re.DOTALL)
        if fenced:
            candidates.append(fenced.group(1))

        # 第一个 { 到最后一个 } 的片段
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            candidates.append(text[start:end + 1])

        for candidate in candidates:
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed

        raise LLMError(f"无法从 LLM 回复中解析 JSON，原文前 200 字: {text[:200]}")
