"""Realtime adapter for the event subset used by oMLX Ask AI Voice.

The adapter deliberately keeps model execution in oMLX. Audio is transcribed
and synthesized through oMLX's OpenAI-compatible audio endpoints; text reasoning
uses /v1/chat/completions.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import re
import uuid
import wave
from contextlib import suppress
from typing import Any

import httpx
from fastapi import WebSocket, WebSocketDisconnect

from .config import get_config
from .conversation import new_id

log = logging.getLogger("omlx-voice-bridge")


def event(kind: str, **kwargs: Any) -> dict[str, Any]:
    return {"type": kind, "event_id": f"evt_{uuid.uuid4().hex[:16]}", **kwargs}


async def send(ws: WebSocket, state: dict[str, Any], kind: str, **kwargs: Any) -> None:
    """Serialize WebSocket writes from the reader and response task."""
    payload = json.dumps(event(kind, **kwargs), ensure_ascii=False, separators=(",", ":"))
    lock: asyncio.Lock = state["send_lock"]
    async with lock:
        await ws.send_text(payload)


def _auth_headers(cfg) -> dict[str, str]:
    return {"Authorization": f"Bearer {cfg.omlx.api_key}"} if cfg.omlx.api_key else {}


def _extract_message_text(message: dict[str, Any]) -> str:
    """Get user-facing answer text without accidentally speaking reasoning."""
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        pieces: list[str] = []
        for part in content:
            if isinstance(part, dict) and part.get("type") in {"text", "output_text"}:
                text = part.get("text")
                if isinstance(text, str):
                    pieces.append(text)
        return "".join(pieces).strip()
    return ""


def _normalise_audio_bytes(resp: httpx.Response, cfg) -> bytes:
    """Return raw mono PCM16 at the rate expected by Ask AI (24 kHz)."""
    data = resp.content
    if not data:
        raise RuntimeError("oMLX speech endpoint returned an empty audio body")

    content_type = resp.headers.get("content-type", "").lower()
    if data[:4] == b"RIFF" or "wav" in content_type or "wave" in content_type:
        try:
            with wave.open(io.BytesIO(data), "rb") as wav:
                channels = wav.getnchannels()
                sample_width = wav.getsampwidth()
                sample_rate = wav.getframerate()
                compression = wav.getcomptype()
                frames = wav.readframes(wav.getnframes())
        except (wave.Error, EOFError) as exc:
            raise RuntimeError(f"oMLX returned an invalid WAV audio body: {exc}") from exc
        if compression != "NONE" or sample_width != 2 or channels != 1:
            raise RuntimeError(
                "oMLX returned WAV audio that is not uncompressed mono PCM16 "
                f"(channels={channels}, sample_width={sample_width}, compression={compression})"
            )
        if sample_rate != cfg.audio.client_sample_rate:
            raise RuntimeError(
                "oMLX speech WAV sample rate does not match Ask AI: "
                f"{sample_rate} Hz returned, {cfg.audio.client_sample_rate} Hz required"
            )
        data = frames

    if len(data) % 2:
        raise RuntimeError(f"oMLX returned an odd-length PCM16 payload ({len(data)} bytes)")
    return data


def _image_data_url(part: dict[str, Any], cfg) -> str:
    """Validate an Ask AI input_image and return its unchanged data URL."""
    value = part.get("image_url")
    if isinstance(value, dict):
        value = value.get("url")
    if not isinstance(value, str):
        raise ValueError("input_image is missing its image_url data URL")
    match = re.fullmatch(r"data:(image/(?:jpeg|png|webp));base64,([A-Za-z0-9+/=]+)", value)
    if not match:
        raise ValueError("Only base64 JPEG, PNG, and WebP input_image data URLs are supported")
    try:
        raw = base64.b64decode(match.group(2), validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise ValueError("input_image contains invalid base64 data") from exc
    vision_cfg = getattr(cfg, "vision", None)
    max_bytes = getattr(vision_cfg, "max_image_bytes", 8 * 1024 * 1024)
    if len(raw) > max_bytes:
        raise ValueError(f"input_image exceeds the {max_bytes // (1024 * 1024)} MiB limit")
    if not raw:
        raise ValueError("input_image is empty")
    return value


def _vision_enabled(cfg) -> bool:
    return bool(getattr(getattr(cfg, "vision", None), "enabled", True))


def realtime_capabilities(cfg) -> dict[str, bool]:
    # The bridge implements image forwarding through the reasoning model. Tool
    # calling is deliberately not advertised because this bridge has no tool loop.
    return {"input_image": _vision_enabled(cfg), "tools": False}


def _max_images_per_turn(cfg) -> int:
    return max(1, int(getattr(getattr(cfg, "vision", None), "max_images_per_turn", 4)))


async def queue_conversation_item(
    ws: WebSocket,
    state: dict[str, Any],
    item: dict[str, Any],
    cfg,
) -> None:
    """Queue the images/text Ask AI sends between audio commit and response.create."""
    item_type = item.get("type", "message")
    item_id = item.get("id") or new_id("item")
    if item_type != "message":
        await send(ws, state, "error", error={
            "type": "unsupported_event", "message": f"Unsupported conversation item type: {item_type}"
        })
        return

    content = item.get("content", [])
    if not isinstance(content, list):
        await send(ws, state, "error", error={
            "type": "invalid_request_error", "message": "conversation item content must be an array"
        })
        return

    new_text: list[str] = []
    new_images: list[str] = []
    try:
        for part in content:
            if not isinstance(part, dict):
                continue
            part_type = part.get("type")
            if part_type in {"input_text", "text"}:
                text = part.get("text")
                if isinstance(text, str):
                    new_text.append(text)
            elif part_type == "input_image":
                if not _vision_enabled(cfg):
                    raise ValueError("Image input is disabled in the bridge configuration")
                new_images.append(_image_data_url(part, cfg))
    except ValueError as exc:
        await send(ws, state, "error", error={
            "type": "invalid_request_error", "message": str(exc)
        })
        return

    images = state["pending_images"]
    if len(images) + len(new_images) > _max_images_per_turn(cfg):
        await send(ws, state, "error", error={
            "type": "invalid_request_error",
            "message": f"A voice turn can contain at most {_max_images_per_turn(cfg)} images",
        })
        return

    images.extend(new_images)
    state["pending_text"] += "".join(new_text)
    # An image-only item is acknowledged without echoing its potentially large
    # base64 payload back to the Mac client.
    acknowledged = {
        "id": item_id,
        "object": "realtime.item",
        "type": "message",
        "status": "completed",
        "role": item.get("role", "user"),
        "content": ([{"type": "input_text", "text": ""}] if new_text else [])
        + ([{"type": "input_image"}] * len(new_images)),
    }
    await send(ws, state, "conversation.item.created", item=acknowledged)
    if new_images:
        log.info("Queued %d screen/image attachment(s) for the next Qwen turn", len(new_images))


async def transcribe(client: httpx.AsyncClient, pcm: bytes, cfg) -> str:
    if not pcm:
        return ""
    wav_io = io.BytesIO()
    with wave.open(wav_io, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(cfg.audio.client_sample_rate)
        wav.writeframes(pcm)

    resp = await client.post(
        f"{cfg.omlx.base_url.rstrip('/')}/v1/audio/transcriptions",
        data={"model": cfg.models.speech},
        files={"file": ("input.wav", wav_io.getvalue(), "audio/wav")},
        headers=_auth_headers(cfg),
    )
    resp.raise_for_status()
    data = resp.json()
    text = data.get("text") or data.get("transcript") or ""
    return text.strip() if isinstance(text, str) else ""


async def complete(client: httpx.AsyncClient, messages: list[dict[str, Any]], cfg) -> str:
    payload = {
        "model": cfg.models.reasoning,
        "messages": messages,
        "stream": False,
        "reasoning_effort": "low",
    }
    resp = await client.post(
        f"{cfg.omlx.base_url.rstrip('/')}/v1/chat/completions",
        json=payload,
        headers=_auth_headers(cfg),
    )
    resp.raise_for_status()
    data = resp.json()
    try:
        message = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"oMLX chat response has no choices[0].message: {data!r}") from exc
    answer = _extract_message_text(message)
    if not answer:
        finish_reason = data.get("choices", [{}])[0].get("finish_reason")
        raise RuntimeError(
            "Qwen returned no user-facing message text"
            + (f" (finish_reason={finish_reason})" if finish_reason else "")
        )
    return answer


async def synthesize(client: httpx.AsyncClient, text: str, cfg) -> bytes:
    payload = {
        "model": cfg.models.speech,
        "input": text,
        "voice": "female",
        "response_format": "pcm",
    }
    resp = await client.post(
        f"{cfg.omlx.base_url.rstrip('/')}/v1/audio/speech",
        json=payload,
        headers=_auth_headers(cfg),
    )
    resp.raise_for_status()
    return _normalise_audio_bytes(resp, cfg)


async def _finish_response(
    ws: WebSocket,
    state: dict[str, Any],
    active: dict[str, Any],
    status: str,
    *,
    error: dict[str, Any] | None = None,
) -> None:
    """Close every opened output item and send exactly one terminal event."""
    if active["terminal_sent"]:
        return
    response_id = active["response_id"]
    item_id = active["item_id"]
    answer = active.get("answer", "")
    output: list[dict[str, Any]] = []

    if active["item_open"]:
        if active["transcript_started"]:
            await send(
                ws, state, "response.output_audio_transcript.done",
                response_id=response_id, item_id=item_id,
                output_index=0, content_index=0, transcript=answer,
            )
        if active["audio_started"]:
            await send(
                ws, state, "response.output_audio.done",
                response_id=response_id, item_id=item_id,
                output_index=0, content_index=0,
            )
        if active["content_open"]:
            await send(
                ws, state, "response.content_part.done",
                response_id=response_id, item_id=item_id,
                output_index=0, content_index=0,
                part={"type": "audio", "transcript": answer},
            )

        item_status = "completed" if status == "completed" else "incomplete"
        final_item = {
            "id": item_id,
            "object": "realtime.item",
            "type": "message",
            "status": item_status,
            "role": "assistant",
            "content": [{"type": "output_audio", "transcript": answer}],
        }
        await send(
            ws, state, "response.output_item.done",
            response_id=response_id, output_index=0, item=final_item,
        )
        if status == "completed":
            output.append(final_item)
            # Ask AI versions that consume conversation items use this event to
            # persist the transcript in their conversation list.
            await send(
                ws, state, "conversation.item.created",
                item={
                    "id": item_id,
                    "object": "realtime.item",
                    "type": "message",
                    "status": "completed",
                    "role": "assistant",
                    "content": [{"type": "text", "text": answer}],
                },
            )

    response_obj: dict[str, Any] = {
        "id": response_id,
        "object": "realtime.response",
        "status": status,
        "output": output,
    }
    if error:
        response_obj["status_details"] = {"error": error}
    await send(ws, state, "response.done", response=response_obj)
    active["terminal_sent"] = True


async def handle_response(
    ws: WebSocket,
    client: httpx.AsyncClient,
    state: dict[str, Any],
    cfg,
    *,
    pcm: bytes,
    input_item_id: str | None,
    user_text: str,
    image_urls: list[str] | None = None,
) -> None:
    response_id = new_id("resp")
    active = {
        "response_id": response_id,
        "item_id": new_id("msg"),
        "item_open": False,
        "content_open": False,
        "transcript_started": False,
        "audio_started": False,
        "terminal_sent": False,
        "answer": "",
    }
    state["active"] = active
    history = state["history"]

    initial_response = {
        "id": response_id,
        "object": "realtime.response",
        "status": "in_progress",
        "output": [],
        "output_modalities": ["audio", "text"],
    }
    await send(ws, state, "response.created", response=initial_response)
    await send(ws, state, "response.in_progress", response=initial_response)

    try:
        if pcm:
            transcript = await transcribe(client, pcm, cfg)
            if not transcript:
                raise RuntimeError("LFM3 returned an empty transcription")
            if input_item_id:
                await send(
                    ws, state, "conversation.item.created",
                    item={
                        "id": input_item_id,
                        "object": "realtime.item",
                        "type": "message",
                        "status": "completed",
                        "role": "user",
                        "content": [{"type": "input_audio", "transcript": transcript}],
                    },
                )
                await send(
                    ws, state, "conversation.item.input_audio_transcription.completed",
                    item_id=input_item_id, content_index=0, transcript=transcript,
                )
        elif user_text.strip():
            transcript = user_text.strip()
        else:
            raise RuntimeError("response.create received without committed audio or text input")

        # Ask AI sends the screenshot as a separate conversation.item.create
        # immediately before response.create. Combine it with this voice turn
        # so Qwen receives the spoken question and the captured screen together.
        image_urls = image_urls or []
        if image_urls:
            user_content: str | list[dict[str, Any]] = [
                {"type": "text", "text": transcript}
            ]
            user_content.extend(
                {"type": "image_url", "image_url": {"url": url}}
                for url in image_urls
            )
        else:
            user_content = transcript
        history.append({"role": "user", "content": user_content})
        max_messages = cfg.conversation.max_history_turns * 2
        if len(history) > max_messages:
            del history[:-max_messages]

        # Open the assistant audio message before waiting for Qwen. These events
        # are required by the Realtime client to track an active output item.
        item_id = active["item_id"]
        await send(
            ws, state, "response.output_item.added",
            response_id=response_id, output_index=0,
            item={
                "id": item_id, "object": "realtime.item", "type": "message",
                "status": "in_progress", "role": "assistant", "content": [],
            },
        )
        active["item_open"] = True
        await send(
            ws, state, "response.content_part.added",
            response_id=response_id, item_id=item_id,
            output_index=0, content_index=0,
            part={"type": "audio", "transcript": ""},
        )
        active["content_open"] = True

        messages = [{"role": "system", "content": cfg.conversation.system_prompt}]
        messages.extend(history)
        answer = await complete(client, messages, cfg)
        active["answer"] = answer
        history.append({"role": "assistant", "content": answer})
        if len(history) > max_messages:
            del history[:-max_messages]

        await send(
            ws, state, "response.output_audio_transcript.delta",
            response_id=response_id, item_id=item_id,
            output_index=0, content_index=0, delta=answer,
        )
        active["transcript_started"] = True
        await send(
            ws, state, "response.output_audio_transcript.done",
            response_id=response_id, item_id=item_id,
            output_index=0, content_index=0, transcript=answer,
        )
        # Mark transcript done here so _finish_response doesn't emit it twice.
        active["transcript_started"] = False

        audio = await synthesize(client, answer, cfg)
        # The client expects PCM16 and audio/pcm at its configured sample rate.
        chunk_bytes = 48 * 1024
        for offset in range(0, len(audio), chunk_bytes):
            chunk = audio[offset:offset + chunk_bytes]
            # Keep each delta aligned to a 16-bit sample.
            if len(chunk) % 2:
                chunk = chunk[:-1]
            if not chunk:
                continue
            await send(
                ws, state, "response.output_audio.delta",
                response_id=response_id, item_id=item_id,
                output_index=0, content_index=0,
                delta=base64.b64encode(chunk).decode("ascii"),
            )
            active["audio_started"] = True

        if not active["audio_started"]:
            raise RuntimeError("LFM3 returned no playable PCM samples")

        await _finish_response(ws, state, active, "completed")

    except asyncio.CancelledError:
        # Cancelling this task aborts the current HTTPX request as well. The
        # reader waits for this cleanup before accepting the next response.
        with suppress(Exception):
            await _finish_response(ws, state, active, "cancelled")
        return
    except httpx.HTTPStatusError as exc:
        detail = f"oMLX returned HTTP {exc.response.status_code}: {exc.response.text[:400]}"
        log.exception("oMLX request failed")
        with suppress(Exception):
            await send(ws, state, "error", error={"type": "server_error", "message": detail})
        with suppress(Exception):
            await _finish_response(
                ws, state, active, "failed",
                error={"type": "server_error", "message": detail},
            )
    except Exception as exc:
        detail = str(exc)[:500] or exc.__class__.__name__
        log.exception("Realtime response failed")
        with suppress(Exception):
            await send(ws, state, "error", error={"type": "server_error", "message": detail})
        with suppress(Exception):
            await _finish_response(
                ws, state, active, "failed",
                error={"type": "server_error", "message": detail},
            )
    finally:
        if state.get("active") is active:
            state["active"] = None


def _new_state() -> dict[str, Any]:
    return {
        "audio_buffer": bytearray(),
        "committed_audio": b"",
        "committed_item_id": None,
        "pending_text": "",
        "pending_images": [],
        "history": [],
        "send_lock": asyncio.Lock(),
        "response_task": None,
        "active": None,
        "last_audio_duration_ms": 0,
    }


async def _cancel_active(state: dict[str, Any]) -> None:
    task: asyncio.Task | None = state.get("response_task")
    if task and not task.done():
        task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await task
    state["response_task"] = None


async def realtime_endpoint(ws: WebSocket) -> None:
    cfg = get_config()
    await ws.accept()
    state = _new_state()
    timeout = httpx.Timeout(cfg.omlx.timeout_seconds, connect=15.0)
    max_audio_bytes = cfg.audio.max_input_seconds * cfg.audio.client_sample_rate * 2

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            await send(
                ws, state, "session.created",
                session={
                    "id": new_id("sess"),
                    "object": "realtime.session",
                    "model": cfg.models.speech,
                    "modalities": ["text", "audio"],
                    "input_audio_format": "pcm16",
                    "output_audio_format": "pcm16",
                    "input_audio_transcription": {"model": cfg.models.speech},
                    # TalkAIController reads this field from session.created to
                    # decide whether to show its screen-share button.
                    "capabilities": realtime_capabilities(cfg),
                    "turn_detection": None,
                },
            )

            while True:
                msg = json.loads(await ws.receive_text())
                kind = msg.get("type", "")

                if kind == "session.update":
                    await send(ws, state, "session.updated", session=msg.get("session", {}))

                elif kind == "input_audio_buffer.append":
                    try:
                        chunk = base64.b64decode(msg.get("audio", ""), validate=True)
                    except (ValueError, TypeError) as exc:
                        await send(ws, state, "error", error={
                            "type": "invalid_request_error", "message": f"Invalid base64 audio chunk: {exc}"
                        })
                        continue
                    state["audio_buffer"].extend(chunk)
                    if len(state["audio_buffer"]) > max_audio_bytes:
                        state["audio_buffer"].clear()
                        await send(ws, state, "error", error={
                            "type": "invalid_request_error",
                            "message": "Audio input exceeded configured duration limit",
                        })

                elif kind == "input_audio_buffer.commit":
                    if not state["audio_buffer"]:
                        await send(ws, state, "error", error={
                            "type": "invalid_request_error", "message": "Cannot commit an empty audio buffer"
                        })
                        continue
                    if len(state["audio_buffer"]) % 2:
                        state["audio_buffer"].pop()
                    if not state["audio_buffer"]:
                        await send(ws, state, "error", error={
                            "type": "invalid_request_error", "message": "Committed audio contains no complete PCM16 sample"
                        })
                        continue
                    state["committed_audio"] = bytes(state["audio_buffer"])
                    state["audio_buffer"].clear()
                    state["committed_item_id"] = new_id("item")
                    duration_ms = len(state["committed_audio"]) / (cfg.audio.client_sample_rate * 2) * 1000
                    state["last_audio_duration_ms"] = duration_ms
                    # A single commit acknowledgement; transcription uses this
                    # exact same item ID when it completes.
                    await send(
                        ws, state, "input_audio_buffer.committed",
                        item_id=state["committed_item_id"],
                    )

                elif kind == "input_audio_buffer.clear":
                    state["audio_buffer"].clear()
                    state["committed_audio"] = b""
                    state["committed_item_id"] = None
                    state["pending_images"].clear()
                    state["pending_text"] = ""
                    await send(ws, state, "input_audio_buffer.cleared")

                elif kind == "conversation.item.create":
                    item = msg.get("item", {})
                    await queue_conversation_item(ws, state, item, cfg)

                elif kind == "response.create":
                    task = state.get("response_task")
                    if task and not task.done():
                        await send(ws, state, "error", error={
                            "type": "invalid_request_error", "message": "A response is already in progress"
                        })
                        continue
                    pcm = state["committed_audio"]
                    input_item_id = state["committed_item_id"]
                    user_text = state["pending_text"]
                    image_urls = list(state["pending_images"])
                    state["committed_audio"] = b""
                    state["committed_item_id"] = None
                    state["pending_text"] = ""
                    state["pending_images"].clear()
                    state["response_task"] = asyncio.create_task(
                        handle_response(
                            ws, client, state, cfg,
                            pcm=pcm, input_item_id=input_item_id,
                            user_text=user_text, image_urls=image_urls,
                        ),
                        name="omlx-realtime-response",
                    )

                elif kind == "response.cancel":
                    await _cancel_active(state)

                elif kind == "conversation.item.truncate":
                    # Keep only the portion the client reports as having played.
                    # Since the bridge stores transcript text rather than an
                    # audio token cache, approximate by played-duration ratio.
                    if state["history"] and state["history"][-1]["role"] == "assistant":
                        duration = max(state.get("last_audio_duration_ms", 0), 1)
                        ratio = min(max(float(msg.get("audio_end_ms", 0)) / duration, 0.0), 1.0)
                        text = state["history"][-1]["content"]
                        if ratio <= 0:
                            state["history"].pop()
                        elif ratio < 1:
                            state["history"][-1]["content"] = text[:max(1, round(len(text) * ratio))]
                    await send(
                        ws, state, "conversation.item.truncated",
                        item_id=msg.get("item_id", ""),
                        content_index=msg.get("content_index", 0),
                        audio_end_ms=msg.get("audio_end_ms", 0),
                    )

                elif kind == "ping":
                    await send(ws, state, "pong")

                else:
                    await send(ws, state, "error", error={
                        "type": "invalid_request_error", "message": f"Unsupported realtime event: {kind}"
                    })

    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("WebSocket session ended unexpectedly")
        with suppress(Exception):
            await ws.close(code=1011)
    finally:
        await _cancel_active(state)
