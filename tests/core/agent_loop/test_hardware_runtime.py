from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from tests.conftest import build_test_agent_loop, build_test_vibe_config
from vibe.core.agent_loop import AgentLoop
from vibe.core.config import MCPStdio
from vibe.core.tools.manager import ToolManager


@dataclass
class _MCPResult:
    structured: dict[str, Any]
    text: str | None = None


class _MCPPool:
    async def call_tool(self, **kwargs: Any) -> _MCPResult:
        assert kwargs["tool_name"] == "robo_devices"
        return _MCPResult(
            structured={
                "devices": [
                    {
                        "device_id": "vendor-arm-1",
                        "manufacturer": "Vendor",
                        "model": "Arm",
                        "kind": "robot_arm",
                        "actions": [],
                    }
                ]
            }
        )

    async def aclose(self) -> None:
        return None


@pytest.mark.asyncio
async def test_deferred_initialization_registers_mcp_hardware_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    disabled_tools = [
        "robo_devices",
        "robo_connect",
        "robo_arm",
        "robo_disarm",
        "robo_observe",
        "robo_execute",
        "robo_stop",
        "robo_disconnect",
    ]
    config = build_test_vibe_config(
        mcp_servers=[
            MCPStdio(
                name="robo-vendor",
                transport="stdio",
                command=["vendor-mcp"],
                disabled_tools=disabled_tools,
            )
        ]
    )
    pool = _MCPPool()
    monkeypatch.setattr(AgentLoop, "_create_mcp_pool", staticmethod(lambda: pool))
    monkeypatch.setattr(ToolManager, "integrate_all", lambda *_args, **_kwargs: None)
    loop = build_test_agent_loop(config=config, defer_heavy_init=True)

    await loop.wait_until_ready()
    manifests = await loop.hardware_runtime.discover()

    assert {manifest.device_id for manifest in manifests} == {
        "sim-arm-1",
        "vendor-arm-1",
    }
    await loop.aclose()
