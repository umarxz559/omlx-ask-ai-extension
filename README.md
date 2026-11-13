# oMLX Ask Ai Bridge 1.0.0

A local FastAPI bridge that lets the **oMLX Ask AI** app use a remote-server connection while keeping inference on your Mac's oMLX server.

The bridge connects three oMLX-compatible HTTP endpoints:

1. **Speech recognition:** `/v1/audio/transcriptions`
2. **Reasoning and image understanding:** `/v1/chat/completions`
3. **Voice generation:** `/v1/audio/speech`

The bridge does **not** load MLX models or manage model weights. oMLX remains responsible for loading and running the configured models.

## Features

- Voice input and spoken responses through oMLX Ask AI using **Another server**.
- Text conversation items as well as microphone audio.
- Screen sharing: advertises image-input capability to the Mac app and accepts screenshots sent by Ask AI.
- Multimodal reasoning: forwards the spoken/transcribed text and screenshots together to the configured reasoning model.
- JPEG, PNG and WebP screenshot data URLs.
- Up to four images per turn, with an 8 MiB limit per image by default.
- Realtime response lifecycle events, including completed assistant output items.
- Background response processing so the WebSocket can receive cancellation events while inference is running.
- Cancellation of the active HTTP inference request.
- Audio output split into Realtime audio delta events.
- Optional bridge API-key protection.
- The bridge proxies other HTTP API requests to oMLX as well.

## Requirements

- macOS with oMLX installed and running.
- Python **3.11 or newer** (`python3 --version`).
- The reasoning and speech models configured in `config.yaml` must be available to oMLX.
- The oMLX server must expose the HTTP endpoints listed above.
- The Mac running oMLX Ask AI and the bridge must be able to reach the bridge address. For initial local use, both run on the same Mac and the bridge listens on `127.0.0.1`.

> **Important:** Screen sharing requires a vision-capable reasoning model. The bridge can forward screenshots, but it cannot add vision capability to a text-only model.

## Project layout

After extracting the ZIP, the project root should look like this:

```text
omlx-voice-bridge/
├── config.yaml
├── pyproject.toml
├── install.sh
├── uninstall.sh
├── README.md
├── voice_bridge/
│   ├── __init__.py
│   ├── app.py
│   ├── config.py
│   ├── conversation.py
│   ├── proxy.py
│   ├── realtime.py
│   └── security.py
├── tests/
└── launchd/
```

Run all installation and start commands from this project root — the directory containing `pyproject.toml` and `config.yaml`.

## 1. Download and extract the project

Download `omlx-ask-ai-extension.zip` and leave it in your Downloads folder.

Open Terminal and extract it into `~/Applications`:

```bash
mkdir -p ~/Applications
unzip -q ~/Downloads/omlx-ask-ai-extension.zip -d ~/Applications
```

Then enter the project directory:

```bash
cd ~/Applications/omlx-voice-bridge
```

Confirm that you are in the correct directory:

```bash
ls
```

You should see at least `pyproject.toml`, `config.yaml`, `voice_bridge`, and `README.md`.

> **Avoid creating an unnecessary nested folder.** The ZIP already contains a top-level `omlx-voice-bridge` directory. Extract it into `~/Applications`, not into another `~/Applications/omlx-voice-bridge` directory. If you already extracted it and see another `omlx-voice-bridge` folder inside the first one, `cd` into the inner directory — the correct project root is the one containing `pyproject.toml`.

## 2. Create the Python virtual environment

A fresh installation does not contain a `.venv` directory. Create it once from the project root:

```bash
python3 --version
python3 -m venv .venv
```

Activate it:

```bash
source .venv/bin/activate
```

Your Terminal prompt will normally show `(.venv)` when the environment is active.

Upgrade pip and install the bridge and its dependencies:

```bash
python -m pip install --upgrade pip
python -m pip install -e .
```

The `-e .` option installs the project in editable mode and installs the dependencies declared in `pyproject.toml`.

### Alternative: use the installation script

The project also includes an installation script. From the project root, run:

```bash
chmod +x install.sh
./install.sh
```

The script creates `.venv`, activates it, upgrades pip, and installs the project. Do not run this alternative if you have already created the environment and installed the dependencies unless you intentionally want to recreate the environment.

## 3. Configure `config.yaml`

Open the configuration file:

```bash
open -e config.yaml
```

The ZIP includes a `config.yaml` with the project's expected settings. Review the following sections and retain the model IDs that are already working in your oMLX installation.

### oMLX server

```yaml
omlx:
  base_url: http://127.0.0.1:8000
  api_key: ""
  timeout_seconds: 300
```

- `base_url`: the address and port of your oMLX HTTP server. The default is `http://127.0.0.1:8000`.
- `api_key`: set this to your oMLX API key when API authentication is enabled. Leave it empty only when your oMLX server does not require authentication.
- `timeout_seconds`: maximum HTTP request timeout for an inference request. The default is 300 seconds.

### Model IDs

```yaml
models:
  reasoning: YOUR_EXISTING_QWEN_MODEL_ID
  speech: YOUR_EXISTING_LFM3_MODEL_ID
```

These are illustrative placeholders, **not literal values to copy**. Keep the actual model IDs already present in your working `config.yaml`. Model IDs must match the IDs exposed by your oMLX server.

### Vision and screen sharing

```yaml
vision:
  enabled: true
  max_images_per_turn: 4
  max_image_bytes: 8388608
```

- `enabled: true` advertises image-input support to the oMLX Ask AI client and enables screenshot forwarding.
- `max_images_per_turn` sets the maximum number of screenshots accepted in one turn.
- `max_image_bytes` sets the maximum size of each image in bytes. `8388608` is 8 MiB.

Set `enabled: false` to disable image input. Screen sharing also requires a vision-capable reasoning model.

### Audio settings

```yaml
audio:
  client_sample_rate: 24000
  max_input_seconds: 90
  output_mime_type: audio/pcm
```

The defaults match the bridge's expected Ask AI audio format. Do not change these values unless you have verified the audio format expected by your installed oMLX Ask AI client and speech endpoints.

### Conversation settings

```yaml
conversation:
  max_history_turns: 20
  system_prompt: >
    You are a concise, capable voice assistant. Answer naturally for spoken
    conversation. Avoid markdown tables and excessive formatting unless asked.
```

You can adjust the system prompt to change the assistant's response style. Keep voice answers reasonably concise.

### Bridge listener and API key

```yaml
server:
  host: 127.0.0.1
  port: 8010
  api_key: ""
```

The commands in this README explicitly start Uvicorn on `127.0.0.1:8010`. Keep this loopback-only address for initial setup. `server.api_key` is an optional **bridge** key; it is different from `omlx.api_key`, which authenticates requests to the oMLX server. For initial same-Mac use, leave the bridge key empty unless you have configured the Ask AI client to send that key.

> Keep API keys private. Do not paste them into logs, screenshots, or support messages.

After editing `config.yaml`, save the file and restart the bridge. Configuration is read when the application starts.

## 4. Start oMLX

Before starting the bridge:

1. Open oMLX on your Mac.
2. Confirm the oMLX server is running.
3. Confirm the configured reasoning and speech models are available to oMLX.

The bridge does not start oMLX or load models on its own. It sends HTTP requests to the running oMLX server.

## 5. Start the voice bridge

Open Terminal and run:

```bash
cd ~/Applications/omlx-voice-bridge
source .venv/bin/activate
uvicorn voice_bridge.app:app --host 127.0.0.1 --port 8010 --log-level info
```

Keep this Terminal window open while using Ask AI. The bridge stops when you press `Ctrl+C` or close the process.

For more detailed WebSocket diagnostics, use:

```bash
uvicorn voice_bridge.app:app --host 127.0.0.1 --port 8010 --log-level debug
```

Debug logs can contain large WebSocket event payloads. Avoid sharing unredacted logs because they may include private conversation metadata or audio-related data.

## 6. Verify that the bridge is running

Open a **second Terminal window**. You do not need to stop the bridge in the first window.

Check the health endpoint:

```bash
curl -fsS http://127.0.0.1:8010/health
```

A successful response looks similar to:

```json
{
  "status": "ok",
  ...
  ...
  "upstream": "http://127.0.0.1:8000",
  "reasoning_model": "configured-reasoning-model",
  "speech_model": "configured-speech-model"
}
```

This confirms that the bridge process is responding and shows its configured upstream and model IDs. It does **not**, by itself, prove that a model has loaded or that an inference request will succeed.

Check the model status endpoint through the bridge:

```bash
curl -fsS http://127.0.0.1:8010/v1/models/status
```

This request is proxied to oMLX. It is a useful check that the bridge can reach the upstream server. The exact response depends on your oMLX version and model state.

If you configured a non-empty `server.api_key`, requests to proxied API paths also require the bridge key, for example:

```bash
curl -fsS http://127.0.0.1:8010/v1/models/status \
  -H 'Authorization: Bearer YOUR_BRIDGE_API_KEY'
```

Do not confuse the bridge API key with the oMLX API key stored in `omlx.api_key`.

## 7. Connect oMLX Ask AI to the bridge

In the oMLX app:

1. Open **Ask AI**.
2. Open the server selection/settings.
3. Choose **Another server** instead of **This Mac**.
4. Enter this server address:

   ```text
   http://127.0.0.1:8010
   ```

5. Start a **new** Ask AI voice session.
6. Test with a short spoken request, such as: **“Hello, how are you?”**

Expected behavior:

- The microphone input is received by the bridge.
- The speech endpoint transcribes the audio.
- The reasoning model generates a response.
- The speech endpoint generates audio.
- Ask AI receives the transcript and audio response and finishes the Thinking state.

### Test screen sharing

With **Another server** selected:

1. Start a new voice session (do not reuse an old WebSocket session).
2. Confirm the screen-share control is visible.
3. Share a screen or window and ask a question about what is visible.
4. The bridge should receive an `input_image` conversation item and include the screenshot with the spoken text in the reasoning request.

The bridge accepts base64 data URLs for JPEG, PNG, and WebP screenshots. The defaults allow up to four images per turn and 8 MiB per image. If the screen-share control is missing, check `vision.enabled: true`, save `config.yaml`, restart the bridge, and create a new Ask AI session.

## 8. Normal startup after the first installation

You only need to create `.venv` and install dependencies once. For every later launch:

1. Start oMLX.
2. Open Terminal and start the bridge:

```bash
cd ~/Applications/omlx-voice-bridge
source .venv/bin/activate
uvicorn voice_bridge.app:app --host 127.0.0.1 --port 8010 --log-level info
```

3. Open Ask AI and connect using **Another server** at `http://127.0.0.1:8010`.

To stop the bridge, return to its Terminal window and press `Ctrl+C`. To leave the virtual environment after stopping it, run:

```bash
deactivate
```

## 9. Run the automated tests (optional)

From the project root, with the virtual environment active:

```bash
python -m pip install -e '.[dev]'
pytest -q
```

The tests use simulated HTTP responses. They cover Realtime event ordering, response completion, stable input item IDs, cancellation, image capability advertisement, screenshot validation/forwarding, and other bridge behavior. They do not replace a live test with your installed oMLX server and the macOS Ask AI client.

## 10. Troubleshooting

### `source: no such file or directory: .venv/bin/activate`

The virtual environment has not been created, or the command is being run from the wrong directory. Go to the project root — the directory containing `pyproject.toml` — and run:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

If `pyproject.toml` is not shown by `ls`, you are not in the project root. Check whether the ZIP created a nested `omlx-voice-bridge` directory.

### `zsh: command not found: uvicorn`

Activate the project's virtual environment and install the package:

```bash
cd ~/Applications/omlx-voice-bridge
source .venv/bin/activate
python -m pip install -e .
```

Then run the Uvicorn command again.

### `Address already in use`

Another process is already listening on port 8010. Stop the existing bridge with `Ctrl+C` in its Terminal window, or use another port consistently in both the Uvicorn command and the Ask AI server address.

### Bridge health works, but Ask AI cannot connect

- Confirm that the bridge Terminal still shows Uvicorn running.
- Confirm that Ask AI is set to **Another server** and the address is `http://127.0.0.1:8010`.
- Start a new Ask AI session after restarting the bridge.
- Check whether `server.api_key` is set. If it is, the client must send the matching bridge key.

### The screen-share control is missing

- Confirm `vision.enabled: true` in `config.yaml`.
- Restart the bridge after changing the configuration.
- Close and reopen the Ask AI voice session so it receives the new session capabilities.
- Confirm that the reasoning model supports image input.

### Ask AI stays on Thinking or returns no audio

- Check that oMLX is running and reachable at `omlx.base_url`.
- Check that both configured models are available to oMLX.
- Check `curl http://127.0.0.1:8010/health` and `curl http://127.0.0.1:8010/v1/models/status`.
- Restart the bridge after changing `config.yaml`.
- Temporarily run Uvicorn with `--log-level debug` and inspect the events around `response.create` and `response.done`.
- Look for upstream errors from `/v1/audio/transcriptions`, `/v1/chat/completions`, or `/v1/audio/speech` in the bridge terminal.

### `401 Unauthorized`

There are two separate API keys:

- `omlx.api_key`: authenticates the bridge to the oMLX server.
- `server.api_key`: optionally authenticates clients connecting to the bridge.

Check the relevant key and whether authentication is enabled on the corresponding server. Never share the actual key in a log or support request.

### oMLX connection or timeout errors

Check that `omlx.base_url` is correct and that the oMLX server is running. The default timeout is 300 seconds. Increase `omlx.timeout_seconds` only if a legitimate inference takes longer and you have confirmed that the upstream request is still progressing.

### Audio format or sample-rate errors

The bridge expects Ask AI input audio as PCM16 at 24 kHz and normalizes generated audio before returning it. Keep the default `audio` settings unless the client or oMLX endpoint contract has changed.

## 11. Security and network exposure

The recommended initial configuration listens only on `127.0.0.1`, which makes the bridge accessible from the same Mac. Do not expose port 8010 to your LAN or the public internet without first configuring authentication, a firewall, and an appropriately secured network transport (normally TLS through a reverse proxy).

Keep both API keys private. Do not commit `config.yaml` with real credentials to a public repository.

## 12. Known behavior and limitations

- The bridge is an HTTP-based adapter; it does not load models, retain model weights, or implement oMLX's internal model-specific realtime inference engine.
- Audio processing follows a transcription → reasoning → speech-generation flow. It is not token-by-token text generation or fully incremental speech synthesis.
- The bridge advertises screen sharing and forwards screenshot images to the configured reasoning model. Actual image understanding depends on that model supporting vision and on the oMLX chat endpoint accepting multimodal `image_url` content.
- The bridge's health endpoint reports process/configuration status, not proof of successful model inference.
- The automated tests use mocked upstream responses; always perform a real voice and screen-share test after installation or upgrading oMLX.

## 13. Uninstall

To remove only the virtual environment while keeping the project and `config.yaml`:

```bash
cd ~/Applications/omlx-voice-bridge
./uninstall.sh
```

To remove the entire bridge project, first stop the running bridge, then remove its directory:

```bash
rm -rf ~/Applications/omlx-voice-bridge
```

This does not uninstall oMLX or delete models managed by oMLX.
