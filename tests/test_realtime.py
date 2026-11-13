import asyncio
import base64
import json
from types import SimpleNamespace

import httpx
import pytest

from voice_bridge.realtime import (
    _new_state, handle_response, queue_conversation_item, realtime_capabilities, realtime_endpoint,
)


class FakeWebSocket:
    def __init__(self):
        self.messages = []

    async def send_text(self, text):
        self.messages.append(json.loads(text))


def make_config():
    return SimpleNamespace(
        omlx=SimpleNamespace(base_url="http://omlx.test", api_key="", timeout_seconds=30),
        models=SimpleNamespace(reasoning="qwen-test", speech="lfm-test"),
        audio=SimpleNamespace(client_sample_rate=24000, max_input_seconds=90),
        conversation=SimpleNamespace(max_history_turns=8, system_prompt="Be concise."),
        vision=SimpleNamespace(enabled=True, max_images_per_turn=4, max_image_bytes=8 * 1024 * 1024),
    )


@pytest.mark.asyncio
async def test_full_audio_turn_emits_complete_realtime_lifecycle():
    cfg = make_config()

    async def handler(request):
        if request.url.path.endswith("/v1/audio/transcriptions"):
            return httpx.Response(200, json={"text": "Hello"})
        if request.url.path.endswith("/v1/chat/completions"):
            return httpx.Response(200, json={
                "choices": [{"message": {"content": "Hello there."}}]
            })
        if request.url.path.endswith("/v1/audio/speech"):
            return httpx.Response(200, content=b"\x00\x00" * 1200,
                                  headers={"content-type": "audio/pcm"})
        return httpx.Response(404)

    ws = FakeWebSocket()
    state = _new_state()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await handle_response(
            ws, client, state, cfg,
            pcm=b"\x00\x00" * 240,
            input_item_id="item_input_1",
            user_text="",
        )

    kinds = [message["type"] for message in ws.messages]
    assert kinds.count("response.done") == 1
    assert kinds.count("response.output_item.added") == 1
    assert kinds.count("response.output_item.done") == 1
    assert kinds.count("response.content_part.added") == 1
    assert kinds.count("response.content_part.done") == 1
    assert kinds.count("input_audio_buffer.committed") == 0  # emitted by commit handler, not response worker
    assert kinds.index("response.output_item.added") < kinds.index("response.output_audio.delta")
    assert kinds.index("response.content_part.done") < kinds.index("response.output_item.done") < kinds.index("response.done")

    done = next(m for m in ws.messages if m["type"] == "response.done")
    assert done["response"]["status"] == "completed"
    assert len(done["response"]["output"]) == 1
    assert done["response"]["output"][0]["content"][0]["transcript"] == "Hello there."

    input_created = next(
        m for m in ws.messages
        if m["type"] == "conversation.item.created" and m["item"]["role"] == "user"
    )
    transcription = next(
        m for m in ws.messages
        if m["type"] == "conversation.item.input_audio_transcription.completed"
    )
    assert input_created["item"]["id"] == transcription["item_id"] == "item_input_1"


@pytest.mark.asyncio
async def test_cancel_emits_one_cancelled_terminal_event():
    cfg = make_config()
    chat_started = asyncio.Event()

    async def handler(request):
        if request.url.path.endswith("/v1/chat/completions"):
            chat_started.set()
            await asyncio.Event().wait()
        return httpx.Response(404)

    ws = FakeWebSocket()
    state = _new_state()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        task = asyncio.create_task(handle_response(
            ws, client, state, cfg,
            pcm=b"", input_item_id=None, user_text="Hi",
        ))
        await asyncio.wait_for(chat_started.wait(), timeout=2)
        task.cancel()
        await asyncio.wait_for(task, timeout=2)

    done_events = [m for m in ws.messages if m["type"] == "response.done"]
    assert len(done_events) == 1
    assert done_events[0]["response"]["status"] == "cancelled"
    assert not any(m["type"] == "error" for m in ws.messages)


@pytest.mark.asyncio
async def test_empty_qwen_response_fails_and_still_closes_response():
    cfg = make_config()

    async def handler(request):
        if request.url.path.endswith("/v1/chat/completions"):
            return httpx.Response(200, json={"choices": [{"message": {"content": ""}}]})
        return httpx.Response(404)

    ws = FakeWebSocket()
    state = _new_state()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await handle_response(
            ws, client, state, cfg,
            pcm=b"", input_item_id=None, user_text="Hi",
        )

    done_events = [m for m in ws.messages if m["type"] == "response.done"]
    assert len(done_events) == 1
    assert done_events[0]["response"]["status"] == "failed"
    assert any(m["type"] == "error" for m in ws.messages)


@pytest.mark.asyncio
async def test_screen_image_is_queued_and_forwarded_to_qwen_with_voice_text():
    cfg = make_config()
    ws = FakeWebSocket()
    state = _new_state()
    image_bytes = b"fake-jpeg-image-data"
    image_url = "data:image/jpeg;base64," + base64.b64encode(image_bytes).decode("ascii")

    await queue_conversation_item(
        ws, state,
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_image", "image_url": image_url}],
        },
        cfg,
    )

    assert state["pending_images"] == [image_url]
    ack = next(m for m in ws.messages if m["type"] == "conversation.item.created")
    assert ack["item"]["status"] == "completed"
    assert image_url not in json.dumps(ack)  # don't echo a large screenshot to the client

    captured = {}

    async def handler(request):
        if request.url.path.endswith("/v1/chat/completions"):
            captured.update(json.loads(request.content))
            return httpx.Response(200, json={
                "choices": [{"message": {"content": "I can see your screen."}}]
            })
        if request.url.path.endswith("/v1/audio/speech"):
            return httpx.Response(200, content=b"\x00\x00" * 1200,
                                  headers={"content-type": "audio/pcm"})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await handle_response(
            ws, client, state, cfg,
            pcm=b"", input_item_id=None, user_text="What is on my screen?",
            image_urls=list(state["pending_images"]),
        )

    user_message = captured["messages"][-1]
    assert user_message["role"] == "user"
    assert user_message["content"][0] == {"type": "text", "text": "What is on my screen?"}
    assert user_message["content"][1] == {
        "type": "image_url", "image_url": {"url": image_url}
    }
    assert "I can see your screen." in json.dumps(ws.messages)


def test_realtime_session_advertises_image_capability_for_screen_share():
    cfg = make_config()
    assert realtime_capabilities(cfg) == {"input_image": True, "tools": False}
    cfg.vision.enabled = False
    assert realtime_capabilities(cfg) == {"input_image": False, "tools": False}


@pytest.mark.asyncio
async def test_invalid_or_oversized_screen_image_is_rejected():
    cfg = make_config()
    cfg.vision.max_image_bytes = 4
    ws = FakeWebSocket()
    state = _new_state()
    image_url = "data:image/jpeg;base64," + base64.b64encode(b"too-large").decode("ascii")

    await queue_conversation_item(
        ws, state,
        {"type": "message", "role": "user", "content": [
            {"type": "input_image", "image_url": image_url}
        ]},
        cfg,
    )

    assert state["pending_images"] == []
    error = next(m for m in ws.messages if m["type"] == "error")
    assert "exceeds" in error["error"]["message"]


@pytest.mark.asyncio
async def test_realtime_session_created_exposes_input_image_capability(monkeypatch):
    from fastapi import WebSocketDisconnect
    import voice_bridge.realtime as realtime

    class EndpointSocket(FakeWebSocket):
        async def accept(self):
            return None

        async def receive_text(self):
            raise WebSocketDisconnect(code=1000)

        async def close(self, code=1000):
            return None

    cfg = make_config()
    monkeypatch.setattr(realtime, "get_config", lambda: cfg)
    ws = EndpointSocket()
    await realtime_endpoint(ws)

    created = next(m for m in ws.messages if m["type"] == "session.created")
    assert created["session"]["capabilities"] == {
        "input_image": True,
        "tools": False,
    }
