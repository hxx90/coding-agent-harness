"""Model adapters for the product API and deterministic automated tests."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
import uuid
from abc import ABC, abstractmethod
from collections import deque
from typing import Any, Deque, Dict, Iterable, List, Optional

from .domain import ModelResponse, NormalizedToolCall
from .errors import ModelAPIError
from .trace import Redactor


MAX_COMPLETION_ATTEMPTS = 3


class ModelAdapter(ABC):
    name = "model"

    @abstractmethod
    def complete(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        timeout_seconds: int = 60,
    ) -> ModelResponse:
        raise NotImplementedError


class ScriptedModelAdapter(ModelAdapter):
    """Deterministic adapter used by integration tests."""

    name = "scripted"

    def __init__(self, responses: Iterable[ModelResponse]) -> None:
        self.responses: Deque[ModelResponse] = deque(responses)
        self.requests: List[Dict[str, Any]] = []

    def complete(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        timeout_seconds: int = 60,
    ) -> ModelResponse:
        self.requests.append({"messages": messages, "tools": tools})
        if not self.responses:
            raise ModelAPIError(
                "script_exhausted",
                "ScriptedModelAdapter 没有更多响应",
                recoverable=False,
            )
        return self.responses.popleft()


class OpenAICompatibleAdapter(ModelAdapter):
    """Minimal OpenAI chat-completions compatible adapter without an SDK."""

    name = "openai-compatible"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        temperature: float = 0.1,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> None:
        if not base_url.strip() or not model.strip():
            raise ValueError("base_url 和 model 不能为空")
        normalized = base_url.rstrip("/")
        if normalized.endswith("/chat/completions"):
            self.url = normalized
            self.models_url = normalized[: -len("/chat/completions")] + "/models"
        else:
            self.url = normalized + "/chat/completions"
            self.models_url = normalized + "/models"
        self.api_key = api_key
        self.model = model
        self.name = "openai-compatible/" + model
        self.temperature = temperature
        self.extra_headers = extra_headers or {}
        self.redactor = Redactor([api_key] if api_key else [])

    def _headers(self) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            **self.extra_headers,
        }
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        return headers

    def list_models(self, timeout_seconds: int = 15) -> List[str]:
        """Read the provider's OpenAI-compatible model catalog."""
        request = urllib.request.Request(
            self.models_url,
            headers=self._headers(),
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                parsed = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code in {401, 403}:
                raise ModelAPIError(
                    "model_auth_error",
                    "模型鉴权失败，请检查 API Key",
                    recoverable=True,
                    details={"status": exc.code},
                ) from exc
            raise ModelAPIError(
                "model_service_error",
                "读取模型列表失败：HTTP %s" % exc.code,
                recoverable=True,
                details={"status": exc.code},
            ) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise ModelAPIError(
                "model_service_error",
                "无法连接模型服务：%s" % exc,
                recoverable=True,
            ) from exc
        except (json.JSONDecodeError, TypeError) as exc:
            raise ModelAPIError(
                "model_response_invalid",
                "模型列表格式无法解析：%s" % exc,
                recoverable=True,
            ) from exc

        data = parsed.get("data") if isinstance(parsed, dict) else None
        if not isinstance(data, list):
            raise ModelAPIError(
                "model_response_invalid",
                "模型列表响应缺少 data 数组",
                recoverable=True,
            )
        return sorted(
            {
                str(item["id"])
                for item in data
                if isinstance(item, dict) and item.get("id")
            }
        )

    @classmethod
    def from_environment(cls) -> "OpenAICompatibleAdapter":
        return cls(
            base_url=os.environ.get("AGENT_HARNESS_BASE_URL", ""),
            api_key=os.environ.get("AGENT_HARNESS_API_KEY", ""),
            model=os.environ.get("AGENT_HARNESS_MODEL", ""),
            temperature=float(os.environ.get("AGENT_HARNESS_TEMPERATURE", "0.1")),
        )

    def complete(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        timeout_seconds: int = 60,
    ) -> ModelResponse:
        payload = {
            "model": self.model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "temperature": self.temperature,
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = self._headers()

        last_error: Optional[ModelAPIError] = None
        for attempt in range(MAX_COMPLETION_ATTEMPTS):
            request = urllib.request.Request(
                self.url,
                data=body,
                headers=headers,
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                    raw = response.read().decode("utf-8")
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise ModelAPIError(
                        "model_response_invalid",
                        "模型 HTTP 响应不是单个 JSON 对象：%s" % exc,
                        recoverable=True,
                        details={
                            "stage": "response_body",
                            "response_preview": self.redactor.text(raw[:1000]),
                        },
                    ) from exc
                return self._normalize(parsed)
            except urllib.error.HTTPError as exc:
                response_text = ""
                try:
                    response_text = self.redactor.text(
                        exc.read().decode("utf-8", errors="replace")[:2000]
                    )
                except Exception:
                    pass
                if exc.code in {401, 403}:
                    raise ModelAPIError(
                        "model_auth_error",
                        "模型鉴权失败，请检查 API Key",
                        recoverable=True,
                        details={"status": exc.code},
                    ) from exc
                recoverable = exc.code == 429 or exc.code >= 500
                code = "model_rate_limited" if exc.code == 429 else "model_service_error"
                last_error = ModelAPIError(
                    code,
                    "模型服务返回 HTTP %s" % exc.code,
                    recoverable=recoverable,
                    details={
                        "status": exc.code,
                        "response": response_text,
                        "attempt": attempt + 1,
                        "max_attempts": MAX_COMPLETION_ATTEMPTS,
                    },
                )
                if not recoverable:
                    raise last_error from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                last_error = ModelAPIError(
                    "model_service_error",
                    "无法连接模型服务：%s" % exc,
                    recoverable=True,
                    details={
                        "attempt": attempt + 1,
                        "max_attempts": MAX_COMPLETION_ATTEMPTS,
                    },
                )
            except ModelAPIError:
                raise
            except (KeyError, IndexError, TypeError) as exc:
                raise ModelAPIError(
                    "model_response_invalid",
                    "模型响应格式无法解析：%s" % exc,
                    recoverable=True,
                    details={"stage": "response_schema"},
                ) from exc
            if attempt < MAX_COMPLETION_ATTEMPTS - 1:
                time.sleep(0.5 * (2**attempt))
        assert last_error is not None
        raise ModelAPIError(
            last_error.code,
            "%s（已自动尝试 %s 次）"
            % (last_error.message, MAX_COMPLETION_ATTEMPTS),
            recoverable=last_error.recoverable,
            details={
                **last_error.details,
                "attempts": MAX_COMPLETION_ATTEMPTS,
            },
        ) from last_error

    @staticmethod
    def _tool_arguments(value: Any, tool_name: str) -> Dict[str, Any]:
        if isinstance(value, dict):
            return value
        if not isinstance(value, str):
            raise ModelAPIError(
                "model_response_invalid",
                "工具 %s 的 arguments 不是对象或 JSON 字符串" % tool_name,
                recoverable=True,
                details={"stage": "tool_arguments", "tool_name": tool_name},
            )

        raw = value.strip()
        if raw.startswith("```json") and raw.endswith("```"):
            raw = raw[7:-3].strip()
        elif raw.startswith("```") and raw.endswith("```"):
            raw = raw[3:-3].strip()

        decoder = json.JSONDecoder()
        objects: List[Dict[str, Any]] = []
        cursor = 0
        try:
            while cursor < len(raw):
                while cursor < len(raw) and (raw[cursor].isspace() or raw[cursor] == ","):
                    cursor += 1
                if cursor >= len(raw):
                    break
                parsed, cursor = decoder.raw_decode(raw, cursor)
                if not isinstance(parsed, dict):
                    raise TypeError("JSON fragment is not an object")
                objects.append(parsed)
        except (json.JSONDecodeError, TypeError) as exc:
            raise ModelAPIError(
                "model_response_invalid",
                "工具 %s 的 arguments 不是有效 JSON 对象：%s" % (tool_name, exc),
                recoverable=True,
                details={
                    "stage": "tool_arguments",
                    "tool_name": tool_name,
                    "arguments_preview": raw[:1000],
                },
            ) from exc

        if not objects:
            raise ModelAPIError(
                "model_response_invalid",
                "工具 %s 的 arguments 为空" % tool_name,
                recoverable=True,
                details={
                    "stage": "tool_arguments",
                    "tool_name": tool_name,
                    "arguments_preview": raw[:1000],
                },
            )

        merged: Dict[str, Any] = {}
        for fragment in objects:
            merged.update(fragment)
        return merged

    @staticmethod
    def _normalize(payload: Dict[str, Any]) -> ModelResponse:
        choice = payload["choices"][0]
        message = choice["message"]
        calls: List[NormalizedToolCall] = []
        for raw_call in message.get("tool_calls") or []:
            function = raw_call.get("function") or {}
            tool_name = str(function.get("name") or "")
            arguments = OpenAICompatibleAdapter._tool_arguments(
                function.get("arguments", {}),
                tool_name or "<unknown>",
            )
            calls.append(
                NormalizedToolCall(
                    call_id=str(raw_call.get("id") or uuid.uuid4()),
                    name=tool_name,
                    arguments=arguments,
                )
            )
        content = message.get("content")
        if content is not None and not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)
        return ModelResponse(
            text=content,
            tool_calls=calls,
            stop_reason=str(choice.get("finish_reason") or "unknown"),
            usage=payload.get("usage"),
            raw_response_id=payload.get("id"),
            provider_message_fields={
                key: message[key]
                for key in ("reasoning_details", "reasoning_content")
                if key in message and message[key] is not None
            },
        )


def _call(name: str, arguments: Dict[str, Any]) -> ModelResponse:
    return ModelResponse(
        tool_calls=[
            NormalizedToolCall(
                call_id="demo-" + str(uuid.uuid4()),
                name=name,
                arguments=arguments,
            )
        ],
        stop_reason="tool_calls",
    )


class DemoModelAdapter(ModelAdapter):
    """Rule-based test adapter; intentionally not exposed in the product UI."""

    name = "built-in-demo"

    USERNAME_FIRST_PATCH = """--- a/src/user.py
+++ b/src/user.py
@@ -1,4 +1,4 @@
 def validate_username(username: str | None) -> str:
-    if username is None:
+    if username is None or username == "":
         raise ValueError("username is required")
     return username
"""

    USERNAME_REPAIR_PATCH = """--- a/src/user.py
+++ b/src/user.py
@@ -1,4 +1,4 @@
 def validate_username(username: str | None) -> str:
-    if username is None or username == "":
+    if username is None or not username.strip():
         raise ValueError("username is required")
     return username
"""

    DISCOUNT_PATCH = """--- a/src/order.py
+++ b/src/order.py
@@ -1 +1,11 @@
 \"\"\"Order domain helpers.\"\"\"
+
+
+def calculate_discount(subtotal: float) -> float:
+    if subtotal < 0:
+        raise ValueError("subtotal must be non-negative")
+    if subtotal < 100:
+        return 0.0
+    if subtotal < 500:
+        return round(subtotal * 0.05, 2)
+    return round(subtotal * 0.10, 2)
"""

    def __init__(self) -> None:
        self.step = 0
        self.mode: Optional[str] = None

    def _task_text(self, messages: List[Dict[str, Any]]) -> str:
        for message in messages:
            if message.get("role") == "user":
                return str(message.get("content") or "")
        return ""

    def complete(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        timeout_seconds: int = 60,
    ) -> ModelResponse:
        if self.mode is None:
            task = self._task_text(messages).lower()
            if "用户名" in task or "username" in task:
                self.mode = "username"
            elif "折扣" in task or "discount" in task:
                self.mode = "discount"
            else:
                self.mode = "unsupported"

        if self.mode == "username":
            responses = [
                _call("list_files", {"directory": "", "max_results": 100}),
                _call("read_file", {"path": "src/user.py", "start_line": 1, "end_line": 100}),
                _call(
                    "submit_plan",
                    {
                        "task_summary": "补全用户名必填校验",
                        "relevant_files": ["src/user.py", "tests/test_user.py"],
                        "planned_changes": ["让 None、空字符串和纯空白用户名均抛出 ValueError"],
                        "expected_modified_files": ["src/user.py"],
                        "validation_plan": "运行项目预设 pytest",
                        "risks_or_questions": [],
                    },
                ),
                _call("apply_patch", {"patch": self.USERNAME_FIRST_PATCH}),
                _call("run_validation", {}),
                _call("apply_patch", {"patch": self.USERNAME_REPAIR_PATCH}),
                _call("run_validation", {}),
            ]
        elif self.mode == "discount":
            responses = [
                _call("list_files", {"directory": "", "max_results": 100}),
                _call("read_file", {"path": "README.md", "start_line": 1, "end_line": 200}),
                _call("read_file", {"path": "src/order.py", "start_line": 1, "end_line": 100}),
                _call(
                    "submit_plan",
                    {
                        "task_summary": "按照 README 实现订单折扣计算函数",
                        "relevant_files": ["README.md", "src/order.py", "tests/test_order.py"],
                        "planned_changes": ["在 src/order.py 新增 calculate_discount 并处理边界值"],
                        "expected_modified_files": ["src/order.py"],
                        "validation_plan": "运行项目预设 pytest",
                        "risks_or_questions": [],
                    },
                ),
                _call("apply_patch", {"patch": self.DISCOUNT_PATCH}),
                _call("run_validation", {}),
            ]
        else:
            responses = [
                _call("list_files", {"directory": "", "max_results": 100}),
                _call(
                    "submit_plan",
                    {
                        "task_summary": "测试 Adapter 无法处理该自定义任务",
                        "relevant_files": [],
                        "planned_changes": ["切换到 OpenAI-compatible 模型后执行真实任务"],
                        "expected_modified_files": [],
                        "validation_plan": "未执行",
                        "risks_or_questions": ["测试 Adapter 仅支持 E01 和 E02"],
                    },
                ),
                _call(
                    "report_blocked",
                    {
                        "reason": "测试 Adapter 仅支持两个验收项目",
                        "attempted": ["读取项目文件列表"],
                        "suggested_next_step": "配置通用 Tool Calling 模型",
                    },
                ),
            ]

        if self.step >= len(responses):
            raise ModelAPIError(
                "demo_exhausted",
                "测试 Adapter 的预设步骤已结束",
                recoverable=False,
            )
        response = responses[self.step]
        self.step += 1
        return response
