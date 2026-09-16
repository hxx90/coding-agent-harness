from __future__ import annotations

import argparse
import ast
import asyncio
from dataclasses import dataclass, field
import os
import re
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from vibe.capx._agent_port import CapXCompletionAgent
from vibe.capx.models import CapXCompletionRequest, CapXMessage
from vibe.utils.http import VibeAsyncHTTPClient

_CODE_BLOCK = re.compile(r"```(?:python)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


class LocalBenchAttempt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    stdout: str
    stderr: str


class LocalBenchResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    success: bool
    turns: int
    folded_cloths: list[str]
    attempts: list[LocalBenchAttempt]


@dataclass
class _Scene:
    folded_cloths: list[str] = field(default_factory=list)


class LocalCaPXBench:
    def __init__(self, agent: CapXCompletionAgent, *, max_turns: int = 3) -> None:
        self._agent = agent
        self._max_turns = max_turns

    async def run(self) -> LocalBenchResult:
        run_id = f"local-capx-{uuid4().hex}"
        scene = _Scene()
        attempts: list[LocalBenchAttempt] = []
        messages = [
            CapXMessage(
                role="system",
                content=(
                    "You are controlling a tiny CaP-X-compatible simulator. "
                    "Return Python in a fenced code block."
                ),
            ),
            CapXMessage(
                role="user",
                content=(
                    "Fold the shirt. Available API:\n"
                    "def fold_cloth(name: str) -> None\n"
                    "def print(message: str) -> None"
                ),
            ),
        ]
        for turn in range(1, self._max_turns + 1):
            request = CapXCompletionRequest(
                model="robo-harness", messages=list(messages)
            )
            response = await self._agent.complete(request, run_id)
            code = _extract_code(response)
            stdout, stderr = _execute_safe(code, scene)
            attempts.append(LocalBenchAttempt(code=code, stdout=stdout, stderr=stderr))
            if "shirt" in scene.folded_cloths:
                return LocalBenchResult(
                    run_id=run_id,
                    success=True,
                    turns=turn,
                    folded_cloths=scene.folded_cloths,
                    attempts=attempts,
                )
            messages.extend([
                CapXMessage(role="assistant", content=response),
                CapXMessage(
                    role="user",
                    content=(
                        "The code did not complete the task.\n"
                        f"Previous code:\n```python\n{code}\n```\n"
                        f"stdout:\n{stdout or '(empty)'}\n"
                        f"stderr:\n{stderr or '(empty)'}\n"
                        "Reply with REGENERATE and corrected fenced Python code."
                    ),
                ),
            ])
        return LocalBenchResult(
            run_id=run_id,
            success=False,
            turns=self._max_turns,
            folded_cloths=scene.folded_cloths,
            attempts=attempts,
        )


class CapXEndpointAgent:
    def __init__(self, endpoint: str, *, api_key: str | None = None) -> None:
        self._endpoint = endpoint
        self._api_key = api_key

    async def complete(self, request: CapXCompletionRequest, run_id: str) -> str:
        headers = {"x-capx-trial-id": run_id}
        if self._api_key:
            headers["authorization"] = f"Bearer {self._api_key}"
        async with VibeAsyncHTTPClient(timeout=200) as client:
            response = await client.post(
                self._endpoint,
                headers=headers,
                json=request.model_dump(mode="json", exclude_none=True),
            )
        response.raise_for_status()
        payload = response.json()
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("Robo bridge returned an invalid completion") from exc
        if not isinstance(content, str):
            raise RuntimeError("Robo bridge returned non-text completion content")
        return content


def _extract_code(response: str) -> str:
    match = _CODE_BLOCK.search(response)
    if match is None:
        return response.removeprefix("REGENERATE").strip()
    return match.group(1).strip()


def _execute_safe(code: str, scene: _Scene) -> tuple[str, str]:
    try:
        module = ast.parse(code, mode="exec")
    except SyntaxError as exc:
        return "", f"SyntaxError: {exc.msg}"
    stdout: list[str] = []
    for statement in module.body:
        if not isinstance(statement, ast.Expr) or not isinstance(
            statement.value, ast.Call
        ):
            return "\n".join(stdout), "unsupported syntax in local safe simulator"
        call = statement.value
        if not isinstance(call.func, ast.Name) or call.keywords:
            return "\n".join(stdout), "unsupported call in local safe simulator"
        values = [_literal_string(arg) for arg in call.args]
        if any(value is None for value in values):
            return "\n".join(stdout), "unsupported argument in local safe simulator"
        args = [value for value in values if value is not None]
        match call.func.id, args:
            case "fold_cloth", [cloth]:
                if cloth not in scene.folded_cloths:
                    scene.folded_cloths.append(cloth)
            case "print", [message]:
                stdout.append(message)
            case _:
                return "\n".join(stdout), (
                    f"unsupported API call in local safe simulator: {call.func.id}"
                )
    return "\n".join(stdout), ""


def _literal_string(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="robo-capx-smoke",
        description="Run a safe two-turn CaP-X-style smoke test against Robo.",
    )
    parser.add_argument("--url", default="http://127.0.0.1:8110/chat/completions")
    parser.add_argument("--api-key-env", default="ROBO_CAPX_API_KEY")
    args = parser.parse_args()
    result = asyncio.run(
        LocalCaPXBench(
            CapXEndpointAgent(args.url, api_key=os.getenv(args.api_key_env))
        ).run()
    )
    print(result.model_dump_json(indent=2))
    raise SystemExit(0 if result.success else 1)


if __name__ == "__main__":
    main()


__all__ = [
    "CapXEndpointAgent",
    "LocalBenchAttempt",
    "LocalBenchResult",
    "LocalCaPXBench",
]
