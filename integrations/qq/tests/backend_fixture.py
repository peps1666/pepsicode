"""Offline model fixture using the real server, agent loop and approval bridge.

No QQ login, network model call, user settings, hooks or user memory are used.
"""

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
os.environ["PEPSI_CODE_MODEL_MODE"] = "mock"

from pepsicode.approval import store as approval_store
from pepsicode.context import memory
from pepsicode.core import session, task_verification
from pepsicode.hooks import HookEngine
from pepsicode.llm.mock_model import MockModelAdapter
from pepsicode.server import app
from pepsicode.types import AgentStep, StreamToken

root = Path(sys.argv[1]).resolve()
session.PEPSI_CODE_DIR = root / "backend-state"
session.SESSIONS_DIR = session.PEPSI_CODE_DIR / "sessions"
approval_store.PEPSI_CODE_PERMISSIONS_PATH = root / "permissions.json"
memory.create_memory_manager = lambda workspace: None
app.create_hook_engine = lambda *args, **kwargs: HookEngine()
app.build_session_prompt = lambda *args, **kwargs: "Offline fixture project prompt"
app.load_runtime_config = lambda cwd: {"model": "fixture", "verificationMode": "always", "toolDiscoveryEnabled": False}


def oversized_workspace(*args, **kwargs):
    raise ValueError("Verification snapshot exceeds 512 MiB")


task_verification.workspace_state = oversized_workspace


class ChatFixtureModel(MockModelAdapter):
    def next(self, messages):
        assert any(message["role"] == "system" and "You are Pepsi" in message["content"] for message in messages)
        index = max(
            i for i, message in enumerate(messages) if message["role"] == "user" and not message.get("internal")
        )
        user = messages[index]["content"]
        results = [message for message in messages[index + 1 :] if message["role"] == "tool_result"]
        if results:
            return AgentStep(type="assistant", content="Pepsi 工具结果：" + results[-1]["content"])
        if user == "请确认操作":
            return AgentStep(
                type="tool_calls",
                calls=[
                    {
                        "id": "fixture-command",
                        "toolName": "run_command",
                        "input": {"command": sys.executable, "args": ["-c", "print('QQ_APPROVED')"]},
                    }
                ],
            )
        if user == "问我问题":
            return AgentStep(
                type="tool_calls",
                calls=[{"id": "fixture-question", "toolName": "ask_user", "input": {"question": "你喜欢什么颜色？"}}],
            )
        count = sum(message["role"] == "user" and not message.get("internal") for message in messages)
        return AgentStep(type="assistant", content=f"Pepsi：{user}（第 {count} 条）")

    def next_stream(self, messages):
        step = self.next(messages)
        if step.content:
            yield StreamToken(type="text", content=step.content)
        for call in step.calls or []:
            yield StreamToken(
                type="tool_use",
                tool_id=call["id"],
                tool_name=call["toolName"],
                tool_input_partial=json.dumps(call["input"]),
            )
        yield StreamToken(type="done", stop_reason="tool_use" if step.calls else "end_turn")


app.MockModelAdapter = ChatFixtureModel
asyncio.run(app.PepsiCodeServer(port=0, token=os.environ["PEPSI_CODE_SERVER_TOKEN"]).start())
