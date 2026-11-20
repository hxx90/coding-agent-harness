"""Operator-selected connection and observation, without model calls or arming."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING
from uuid import uuid4

from vibe.app_server.models import HardwareDeviceOutput, HardwareSnapshotOutput
from vibe.app_server.protocol import HardwareSetupParams, HardwareSetupResponse

if TYPE_CHECKING:
    from vibe.core.agent_loop import AgentLoop


async def setup_robot(
    agent: AgentLoop, params: HardwareSetupParams, selected: str | None
) -> HardwareSetupResponse:
    response = HardwareSetupResponse(selected_device_id=selected)
    try:
        model = agent.config.get_active_model()
        response.model_name = model.display_name or model.name
        agent.config.get_active_provider()
        agent.config.require_active_provider_api_key()
        response.model_configured = True
    except (ValueError, RuntimeError):
        response.issues.append("模型配置或凭据不可用，请检查 /model 和模型服务配置。")
    runtime = agent.hardware_runtime
    try:
        async with asyncio.timeout(90):
            manifests = await runtime.discover()
            response.devices = [
                HardwareDeviceOutput.model_validate(item.model_dump(mode="json"))
                for item in manifests
            ]
            device_ids = {item.device_id for item in manifests}
            if selected in device_ids and selected in runtime.connected_device_ids:
                response.selection_confirmed = True
            elif selected:
                response.issues.append(
                    "上次选择的机器人本次未能确认，请检查连接后重新读取。"
                )
            if not params.connect:
                return response
            if params.device_id not in device_ids:
                response.issues.append("所选机器人已不可用，请刷新列表。")
                return response
            assert params.device_id is not None
            run_id = f"operator-setup-{uuid4().hex}"
            # Opening the picker or refreshing never resets an existing scene.
            if params.device_id not in runtime.connected_device_ids:
                await runtime.connect(params.device_id, run_id=run_id)
            snapshot = await runtime.observe(params.device_id, run_id=run_id)
            response.snapshot = HardwareSnapshotOutput.model_validate(
                snapshot.model_dump(mode="json")
            )
            # Selection is committed by the backend after cancellable I/O settles.
    except Exception as exc:
        # Connection exceptions can include transport environment/credentials.
        # Return a bounded classification, never stringify arbitrary exceptions.
        response.issues.append(
            f"机器人发现或连接失败（{type(exc).__name__}）。请检查 /mcp 中的机器人连接配置。"
        )
    return response


async def commit_robot_selection(agent: AgentLoop, device_id: str) -> None:
    context = (
        "Operator selected robot "
        + json.dumps(device_id)
        + ". It is connected. Use this device for the next robot task; "
        "read its capabilities and obtain a fresh observation before motion. "
        "Robot selection did not acquire a lease or arm the device."
    )
    # Check the actual current history, not only a backend cache that can outlive
    # /clear, rewind or compaction. Repeated observations do not grow context.
    latest = next(
        (
            m.content
            for m in reversed(agent.messages)
            if m.injected
            and isinstance(m.content, str)
            and m.content.startswith("Operator selected robot ")
        ),
        None,
    )
    if latest != context:
        await agent.inject_user_context(context, as_message=False)
