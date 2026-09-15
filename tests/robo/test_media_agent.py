from __future__ import annotations

import json
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from agent_harness.coding.context import request_messages
from agent_harness.coding.media import MediaStore, references, text_content
from agent_harness.coding.provider import Provider
from agent_harness.coding.session import CodingSession
from agent_harness.coding.settings import ProviderSettings, Settings
from agent_harness.coding.storage import SessionStore
from agent_harness.coding.types import CodingError, Completion, ToolCall
from agent_harness.robo.demo import BAD, GOOD, MANIFEST, wait_job
from agent_harness.robo.hardware import png


def settings(tmp_path, host):
    project = tmp_path / "project"
    project.mkdir()
    return Settings(
        project=project,
        data_dir=tmp_path / "sessions",
        provider=ProviderSettings(),
        robo_home=host[0].root,
        max_turns=100,
        permission_rules=({"tool": "*", "action": "allow"},),
    )


def test_media_roundtrip_provider_frames_and_old_sessions(tmp_path):
    store = SessionStore(tmp_path / "data")
    image = store.media.put(
        png(2, 2, bytes([240, 0, 0] * 4)),
        "image/png",
        metadata={"source": "test-camera", "sampled_at": 12, "frame": "camera_px"},
    )
    state = store.create(tmp_path, "image-model")
    state["messages"] = [
        {"role": "user", "content": [{"type": "text", "text": "Inspect frames"}, image]}
    ]
    store.save(state)
    exported = tmp_path / "export.json"
    store.export(state["id"], exported)
    other = SessionStore(tmp_path / "other")
    imported = other.import_session(exported, tmp_path)
    assert other.media.get(image) == store.media.get(image)
    assert imported["messages"] == state["messages"]
    old = store.create(tmp_path, "text-model")
    old["schema_version"] = 1
    old["messages"] = [{"role": "user", "content": "old CLI"}]
    store.save(old)
    assert store.load(old["id"])["messages"] == old["messages"]
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append(
                json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            )
            response = json.dumps(
                {
                    "choices": [
                        {
                            "message": {"content": "frame received"},
                            "finish_reason": "stop",
                        }
                    ]
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        provider = Provider(
            ProviderSettings(
                base_url=f"http://127.0.0.1:{server.server_port}/v1",
                model="test",
                stream=False,
            ),
            other.media,
        )
        messages = [
            *imported["messages"],
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "one",
                        "type": "function",
                        "function": {"name": "observe", "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "one",
                "content": [{"type": "text", "text": "Observation"}, image],
            },
        ]
        result = provider.complete(
            messages, [], on_text=lambda _: None, stop=threading.Event()
        )
        assert result.text == "frame received"
        sent = requests[0]["messages"]
        assert isinstance(sent[-2]["content"], str) and sent[-2]["role"] == "tool"
        assert sent[-1]["content"][-1]["image_url"]["url"].startswith(
            "data:image/png;base64,iVBOR"
        )
        assert "sampled_at" in json.dumps(sent[-1])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    bad = json.loads(exported.read_text())
    bad["media_blobs"][image["id"]] = "AAAA"
    exported.write_text(json.dumps(bad))
    with pytest.raises(CodingError, match="hash mismatch"):
        other.import_session(exported, tmp_path)


def test_multimodal_compaction_preserves_tool_groups(tmp_path):
    image = MediaStore(tmp_path).put(png(2, 2, bytes([0] * 12)), "image/png")
    messages = []
    for i in range(12):
        messages.extend(
            [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": "request " + str(i)}, image],
                },
                {"role": "assistant", "content": "answer " * 100},
            ]
        )
    compacted, changed = request_messages("system", messages, 8000, force=True)
    assert changed
    assert any("request 11" in text_content(m["content"]) for m in compacted)
    assert len(references(compacted)) < 12


class ClosedLoopModel:
    """Deterministic model protocol harness, not a claim about real model skill."""

    def __init__(self):
        self.phase = 0
        self.repair = False
        self.jobs = []
        self.images = []
        self.polls = 0

    def complete(self, messages, tools, *, on_text, stop):
        self.images.extend(references(messages))
        if self.phase == 0:
            self.phase += 1
            return self.call("robo_devices", {})
        if self.phase == 1:
            self.phase += 1
            return self.call("robo_observe", {})
        if self.phase == 2:
            self.phase += 1
            return self.call(
                "write", {"path": "program.json", "content": json.dumps(MANIFEST)}
            )
        if self.phase == 3:
            self.phase += 1
            return self.call("write", {"path": "control.py", "content": BAD})
        if self.phase == 4:
            self.phase += 1
            return self.call("program_publish", {"manifest": "program.json"})
        content = messages[-1]["content"]
        outcome = json.loads(
            content[0]["text"] if isinstance(content, list) else content
        )
        assert "error" not in outcome, outcome
        if self.phase == 5:
            self.version = outcome["id"]
            self.phase += 1
            return self.call("robo_observe", {})
        if self.phase == 6:
            self.phase += 1
            return self.call(
                "program_run",
                {
                    "program_version": self.version,
                    "based_on": outcome["id"],
                    "parameters": {"gain": 0.65},
                    "request_id": "repair" if self.repair else "trial",
                },
            )
        if self.phase == 7:
            self.jobs.append(outcome["id"])
            self.phase += 1
            self.cursor = 0
            return self.call(
                "program_events", {"job_id": self.jobs[-1], "after": 0, "wait": 1}
            )
        if self.phase == 8:
            self.cursor = outcome["cursor"]
            if (
                outcome["job"]["status"] not in {"exited", "failed"}
                or outcome["events"]
            ):
                return self.call(
                    "program_events",
                    {"job_id": self.jobs[-1], "after": self.cursor, "wait": 1},
                )
            if self.repair:
                assert outcome["job"]["physical_verification"]["status"] == "succeeded"
                self.phase = 11
                return self.call("program_status", {"job_id": self.jobs[-1]})
            assert outcome["job"]["physical_verification"]["status"] == "failed"
            self.phase += 1
            return self.call("read", {"path": "control.py"})
        if self.phase == 9:
            self.phase = 4
            self.repair = True
            return self.call(
                "edit", {"path": "control.py", "old_string": BAD, "new_string": GOOD}
            )
        self.polls += 1
        if self.polls < 7:
            return self.call("program_status", {"job_id": self.jobs[-1]})
        return Completion(
            "Simulation physically verified; real MHS remains unavailable."
        )

    @staticmethod
    def call(name, args):
        import uuid

        return Completion(
            calls=[ToolCall(uuid.uuid4().hex, name, json.dumps(args))],
            finish_reason="tool_calls",
            usage={"prompt_tokens": 100, "completion_tokens": 20},
        )


def test_agent_writes_observes_repairs_and_resumes(host, tmp_path):
    config = settings(tmp_path, host)
    model = ClosedLoopModel()
    session = CodingSession(config, model=model)
    result = session.run(
        "Implement local visual alignment and use feedback to repair it"
    )
    assert result.status == "completed", result
    assert len(model.jobs) == 2 and model.images and model.polls == 7
    assert session.state["turns"][-1]["changes"].keys() == {
        "control.py",
        "program.json",
    }
    assert config.project.joinpath("control.py").read_text() == GOOD
    resumed = CodingSession(config, session_id=session.id, model=model)
    assert resumed.state["messages"] == session.state["messages"]
    assert (
        wait_job(host[0], model.jobs[-1])["physical_verification"]["status"]
        == "succeeded"
    )


@pytest.mark.parametrize("cancel_model", [False, True])
def test_model_completion_and_cancellation_do_not_own_job(host, tmp_path, cancel_model):
    config = settings(tmp_path, host)
    client = host[0]
    manifest = {**MANIFEST, "parameters": {}}
    version = client.request(
        "publish",
        manifest=manifest,
        sources={"control.py": 'robo.sleep(1.5)\nprint("survived turn")'},
    )
    scene = client.request("observe")

    class Model:
        def __init__(self):
            self.calls = 0

        def complete(self, messages, tools, *, on_text, stop):
            self.calls += 1
            if self.calls == 1:
                return ClosedLoopModel.call(
                    "program_run",
                    {
                        "program_version": version["id"],
                        "request_id": "independent",
                        "based_on": scene["id"],
                        "parameters": {},
                    },
                )
            if cancel_model:
                from agent_harness.coding.types import Cancelled

                raise Cancelled()
            return Completion("Model has finished; program is still supervised")

    session = CodingSession(config, model=Model())
    result = session.run("Submit the program and end the model turn")
    assert result.status == ("cancelled" if cancel_model else "completed")
    job = client.request("lookup", request_id="independent")
    assert job["status"] == "running"
    assert wait_job(client, job["id"])["exit_code"] == 0


def test_plan_and_credentials_not_needed_for_local_runtime(host, tmp_path):
    config = replace(settings(tmp_path, host), mode="plan")

    class PlanModel:
        def complete(self, messages, tools, *, on_text, stop):
            names = {t["function"]["name"] for t in tools}
            assert "program_run" not in names and "program_cancel" not in names
            return Completion("Plan only")

    session = CodingSession(config, model=PlanModel())
    result = session.run("inspect")
    assert result.status == "completed"
    assert not host[0].request("jobs")["jobs"]


def test_interrupted_submission_recovers_original_job(host, tmp_path):
    config = settings(tmp_path, host)
    client = host[0]
    version = client.request(
        "publish", manifest=MANIFEST, sources={"control.py": "robo.sleep(0.5)"}
    )
    args = {
        "program_version": version["id"],
        "request_id": "lost-reply",
        "parameters": {"gain": 0.5},
        "based_on": client.request("observe")["id"],
    }
    original = client.request("submit", **args)
    session = CodingSession(config)
    session.state["messages"] = [
        {"role": "user", "content": "Submit"},
        Completion(calls=[ToolCall("lost", "program_run", json.dumps(args))]).message(),
    ]
    session._save()
    session._balance_pending("Previous conversation interrupted")
    recovered = json.loads(session.state["messages"][-1]["content"])
    assert recovered["recovered"] and recovered["job"]["id"] == original["id"]
    assert len(client.request("jobs")["jobs"]) == 1
    wait_job(client, original["id"])
