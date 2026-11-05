# Voice Edge - [A]LL [I]n One Script

## Combines

- **Local Apple Silicon AI**: `mlx-lm` and `mlx-vlm` inference, local embeddings, reranking, and FIM code completion
- **Speech & Voice Interaction**: Real-time STT (`faster-whisper` / Apple Speech), streaming Edge-TTS, global macOS dictation, HUD overlay, and local wake-word voice chat
- **Unified OpenAI & MCP Gateway**: Standard OpenAI-compatible HTTP endpoints (`/v1/chat/completions`, `/v1/audio/*`, `/v1/embeddings`, `/v1/rerank`, `/v1/completions`) and MCP server
- **Browser-Backed Model Relays**: DeepSeek, Doubao, Qwen, Microsoft 365 Copilot, and Google AI Studio Playground (`gemini:aistudio/*`), powered by the [voice-edge-auth-sync](https://github.com/woniuzfb/voice-edge-auth-sync) Firefox extension
- **Gemini App Task-Mode Relay**: Native macOS Gemini app Argon models (`gemini:app/*`) with bidirectional `ExecuteRemoteTools` (ERT) streaming for [Voice-Edge Harness (veh)](https://github.com/woniuzfb/voice-edge-harness)
- **Third-Party API Relays**: OpenRouter (`openrouter:*`) and OpenCode (`opencode:zen/*`, `opencode:go/*`) with multi-key round-robin rotation and protocol translation
- **Xiaomi XiaoAI Bridge**: Smart speaker LAN bridge with wake-word routing, streaming TTS playback, and native Xiaomi assistant fallback

## Requirements

- macOS (Apple Silicon)
- Python 3.11 or newer
- FFmpeg
- Microphone permission
- Accessibility permission
- Input Monitoring permission
- [voice-edge-auth-sync](https://github.com/woniuzfb/voice-edge-auth-sync)
- [continue](https://github.com/woniuzfb/continue)
- [voice edge harness](https://github.com/woniuzfb/voice-edge-harness)

## Installation

```bash
brew install ffmpeg
uv sync
uv run python -m camoufox fetch
```

Install the Firefox authentication-sync extension when using browser-backed providers (DeepSeek, Doubao, Qwen, Microsoft 365 Copilot, or Google AI Studio Playground).

Install the Continue extension when using Voice Edge through VS Code.

Install the Voice-Edge Harness (`veh`) when using Gemini App Argon (`gemini:app/*`) or AI Studio Playground (`gemini:aistudio/*`) models in agentic task mode.

## Running

### MCP mode

```bash
uv run main
```

### HTTP mode

```bash
uv run main --http
```

Default ports:

- MCP: `5001`
- HTTP: `5002`

The first startup may take longer while local components initialize.

## OpenAI-Compatible APIs

| Capability       | Endpoint                        |
| ---------------- | ------------------------------- |
| Chat completions | `POST /v1/chat/completions`     |
| Speech-to-text   | `POST /v1/audio/transcriptions` |
| Text-to-speech   | `POST /v1/audio/speech`         |
| Embeddings       | `POST /v1/embeddings`           |
| Rerank           | `POST /v1/rerank`               |
| FIM              | `POST /v1/completions`          |

## Core Features

### Speech Recognition

- Real-time transcription
- Audio and video file transcription
- Global dictation mode
- Apple Speech integration
- Multilingual recognition

### Local AI

- MLX-LM and MLX-VLM backends
- Local embeddings and reranking
- Fill-in-the-middle code completion
- OpenAI-compatible model routing

### Speech Synthesis

- Streaming Edge-TTS playback
- Multiple voice aliases
- Sequential audio queue
- Long-form speech support
- Automatic playback recovery

### macOS Integration

- Native HUD overlay
- Global keyboard shortcuts
- Persistent audio output stream
- Bluetooth and audio-route recovery

### Browser-Backed Providers

Supported browser-backed model families include:

- DeepSeek: default, expert, and vision modes
- Doubao
- Qwen
- Microsoft 365 Copilot
- Google AI Studio Playground (`gemini:aistudio/*`)

Firefox synchronizes local browser authentication state through the Native Messaging bridge and the local WebSocket relay (`ws://127.0.0.1:5003/ws`). Authentication values are treated as secrets and must not be logged, uploaded, or committed.

## Microsoft 365 Copilot and SharePoint

Configure the Microsoft 365 entry page and optional SharePoint upload and download location:

```bash
export M365_ENTRY_URL='https://outlook.cloud.microsoft/host/xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx/entity1-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx'
export SHAREPOINT_HOME_URL='https://tenant.sharepoint.com/sites/site_name'

# The storage location for input files
export SHAREPOINT_UPLOAD_FOLDER='Upload'

# The storage location for model output files
export SHAREPOINT_DOWNLOAD_FOLDER='Download'
```

`SHAREPOINT_UPLOAD_FOLDER` is document-library-relative. Leave it empty to use the library root.

`SHAREPOINT_DOWNLOAD_FOLDER` is document-library-relative. Leave it empty to use `SHAREPOINT_UPLOAD_FOLDER`.

Conversation-state settings:

```bash
# Restore the last locally stored browser conversation.
export VOICE_EDGE_RESTORE_LAST_CONVERSATION=1

# Disable local conversation persistence.
export VOICE_EDGE_DISABLE_CONVERSATION_PERSISTENCE=1

# Unified browser-provider state file.
export VOICE_EDGE_BROWSER_STATE_PATH="$HOME/.voice-edge/browser-provider-state.json"
```

When a supported client uploads files for a Microsoft 365 model, Voice Edge can upload them to the configured SharePoint folder and send them to Microsoft 365 Copilot as file attachments.

## Xiaomi XiaoAI Configuration

Enable the Xiaomi bridge and provide account and device settings:

```bash
export XIAOAI_ENABLED=1
export MI_USER='your_xiaomi_account'
export MI_PASS='your_password'
export XIAOAI_HARDWARE='LX01'
export MI_DID=''
export XIAOAI_OTP_FILE="$HOME/.mi.otp"
export XIAOAI_OTP_TIMEOUT=300
export XIAOAI_OTP_POLL_INTERVAL=0.5
export XIAOAI_WAKEUP_MODE='directive'
```

Select the AI model:

```bash
# Doubao
export XIAOAI_MODEL='LLM:doubao'
export DOUBAO_BROWSER_ENGINE='camoufox'

# Qwen
export XIAOAI_MODEL='LLM:qwen'
export QWEN_BROWSER_MODEL='qwen3.7-plus'

# DeepSeek
export XIAOAI_MODEL='LLM:deepseek'

# Microsoft 365 Copilot example
export XIAOAI_MODEL='LLM:m365-chatgpt-6.1'
```

The system prompt is used by local models that support a system-message role. It is not prepended to browser-model user messages.

```bash
export XIAOAI_SYSTEM_PROMPT='Please keep your answer concise and output text suitable for voice reading only.'
export XIAOAI_MAX_TOKENS=500
export XIAOAI_TEMPERATURE=0.3
```

### Model Routing

```bash
export XIAOAI_TRIGGER_WITHOUT_KEYWORD=1
export XIAOAI_KEYWORDS='help me,please'
export XIAOAI_STOP_PHRASES='stop answering,stop,hold on'
export XIAOAI_NEW_CONVERSATION='Change the subject,new conversation,new chat,clear context,change the topic,start over'
```

Queries matching native keywords bypass the AI model and are handled by the native Xiaomi assistant:

```bash
export XIAOAI_NATIVE_KEYWORDS='weather,time'
export XIAOAI_NATIVE_STATUS_POLL_INTERVAL=0.25
export XIAOAI_NATIVE_PLAY_START_TIMEOUT=3.0
export XIAOAI_NATIVE_PLAY_END_TIMEOUT=30.0
export XIAOAI_NATIVE_IDLE_CONFIRMATIONS=1
export XIAOAI_NATIVE_STATUS_FALLBACK_DELAY=3.0
export XIAOAI_NATIVE_TAIL_GUARD=0.20
```

### Playback and Audio Streaming

```bash
export XIAOAI_VOICE='zh'
export XIAOAI_TTS_SPEED=1.0
export XIAOAI_AUDIO_BIND_HOST='0.0.0.0'
export XIAOAI_AUDIO_PORT=8050
export XIAOAI_AUDIO_PUBLIC_HOST=''
export XIAOAI_PLAYBACK_DRAIN_MARGIN=0.25
export XIAOAI_PLAYBACK_DRAIN_MAX=180
export XIAOAI_PLAYBACK_STATUS_POLL_INTERVAL=0.15
export XIAOAI_PLAYBACK_STATUS_MAX_WAIT=4.0
export XIAOAI_PLAYBACK_IDLE_CONFIRMATIONS=1
export XIAOAI_PLAYBACK_TAIL_GUARD=0.20
export XIAOAI_PLAYBACK_TIMEOUT=300
```

Xiaomi devices fetch the audio stream over the LAN. Do not use `127.0.0.1` as the public host.

### Streaming and Polling

```bash
export XIAOAI_POLL_INTERVAL=0.1
export XIAOAI_POLL_MIN_INTERVAL=0.08
export XIAOAI_POLL_LOG_EVERY=1
export XIAOAI_AUDIO_MAX_BUFFER_BYTES=524288
export XIAOAI_SPEECH_TARGET_CHARS=42
export XIAOAI_HISTORY_TURNS=6
export XIAOAI_MP3_BITRATE='64k'
export XIAOAI_QUERY_DEBOUNCE_SECONDS=4
export XIAOAI_WAKEUP_SUPPRESS_SECONDS=0
```

### Optional Tavily Search

```bash
export TAVILY_API_KEY='your_api_key'
export XIAOAI_TAVILY_TOOL_ENABLED=1
export XIAOAI_TAVILY_TOOL_MAX_RESULTS=3
export XIAOAI_TAVILY_TOOL_TIMEOUT=30
```

### Optional Local miIO

```bash
uv run main --mi-list

export XIAOAI_LOCAL_DEVICES = '[
  {
    "name": "living-room",
    "ip": "192.168.1.101",
    "token": "",
    "hardware": "LX01",
    "mi_did": "",
    "audio_port": 8050
  },
  {
    "name": "bedroom",
    "ip": "192.168.1.102",
    "token": "",
    "hardware": "LX01",
    "mi_did": "",
    "audio_port": 8051
  }
]'
```

## Local Voice Chat

```bash
export VE_VOICE_CHAT_ENABLED=1
export VE_VOICE_CHAT_MODEL_DIR='/path/to/sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20'
export VE_VOICE_CHAT_ALIAS_DEEPSEEK='DeepSeek'
export VE_VOICE_CHAT_ALIAS_DOUBAO='Doubao'
export VE_VOICE_CHAT_HISTORY_CHAR_BUDGET=6000
export VE_VOICE_CHAT_SHOW_HUD=0
export VE_VOICE_CHAT_KWS_ENGINE='apple'
export SPEECH_HELPER_ON_DEVICE=1
export VE_VOICE_CHAT_CAPTURE_LOCALES='zh-CN,en-US'
```

```python
# Single source of truth for wake-up configuration. Each entry defines only one primary keyword; aliases belong exclusively to that keyword.
# command_locale=None indicates that subsequent commands automatically use the first locale that returns valid text.
VE_VOICE_CHAT_WAKE_CONFIG = (
    {
        "keyword": "hello deepseek",
        "aliases": (
            "hello ts",
            "hello ds",
            "hello deep",
            "hello deepa",
            "hello deepak",
            "hello deepa sick",
        ),
        "route": ("model", "LLM:deepseek"),
        "command_locale": "en-US",
        "model_alias": os.getenv("VE_VOICE_CHAT_ALIAS_DEEPSEEK", "DeepSeek"),
        "score": None,
        "threshold": None,
        "apple_threshold": 0.88,
    },
    {
        "keyword": "Hello doubao",
        "aliases": ("Hello baozi",),
        "route": ("model", "LLM:doubao"),
        "command_locale": "en-US",
        "model_alias": os.getenv("VE_VOICE_CHAT_ALIAS_DOUBAO", "豆包"),
        "score": None,
        "threshold": None,
        "apple_threshold": 0.88,
    },
    {
        "keyword": "Change the subject",
        "aliases": (),
        "route": ("command", "new_session"),
        "command_locale": "en-US",
        "model_alias": None,
        "score": None,
        "threshold": None,
        "apple_threshold": 0.88,
    },
)
```

## VSCode Configuration

```yaml
experimental:
  readResponseTTSServer:
    mcpId: "local-tts"
    toolName: "speak"
models:
  - name: Local
    provider: Local
    model: AUTODETECT
    apiBase: http://localhost:5002/v1/
    roles:
      - chat
    capabilities:
      - tool_use
  - name: LLM:m365-claude-opus
    provider: Local
    model: LLM:m365-claude-opus
    apiBase: http://localhost:5002/v1/
    roles:
      - chat
    defaultCompletionOptions:
      contextLength: 1000000
      maxTokens: 128000
    capabilities:
      - tool_use
      - image_input
    excludeToolOutputsFromTokenCount: true
  - name: LLM:m365-chatgpt-6.1
    provider: Local
    model: LLM:m365-chatgpt-6.1
    apiBase: http://localhost:5002/v1/
    roles:
      - chat
    defaultCompletionOptions:
      contextLength: 1050000
      maxTokens: 128000
    capabilities:
      - tool_use
      - image_input
    excludeToolOutputsFromTokenCount: true
  - name: LLM:m365-chatgpt-6
    provider: Local
    model: LLM:m365-chatgpt-6
    apiBase: http://localhost:5002/v1/
    roles:
      - chat
    defaultCompletionOptions:
      contextLength: 1050000
      maxTokens: 128000
    capabilities:
      - tool_use
      - image_input
    excludeToolOutputsFromTokenCount: true
  - name: LLM:m365-chatgpt-5.6
    provider: Local
    model: LLM:m365-chatgpt-5.6
    apiBase: http://localhost:5002/v1/
    roles:
      - chat
    defaultCompletionOptions:
      contextLength: 1050000
      maxTokens: 128000
    capabilities:
      - tool_use
      - image_input
    excludeToolOutputsFromTokenCount: true
  - name: LLM:m365-chatgpt-5.6-quick
    provider: Local
    model: LLM:m365-chatgpt-5.6-quick
    apiBase: http://localhost:5002/v1/
    roles:
      - chat
    defaultCompletionOptions:
      contextLength: 1050000
      maxTokens: 128000
    capabilities:
      - tool_use
      - image_input
    excludeToolOutputsFromTokenCount: true
  - name: embed:jina-v5
    provider: Local
    model: embed:jina-v5
    apiBase: http://localhost:5002/v1/
    roles:
      - embed
  - name: rerank:jina-v3
    provider: Local
    model: embed:jina-v3
    apiBase: http://localhost:5002/v1/
    roles:
      - rerank
  - name: FIM:qwen-2.5-coder-1.5B
    provider: Local
    model: FIM:qwen-2.5-coder-1.5B
    apiBase: http://localhost:5002/v1/
    roles:
      - edit
      - apply
      - autocomplete
  - name: FIM:qwen-2.5-coder-7B
    provider: Local
    model: FIM:qwen-2.5-coder-7B
    apiBase: http://localhost:5002/v1/
    roles:
      - edit
      - apply
      - autocomplete
```

## Keyboard Shortcuts

| Shortcut             | Action                          |
| -------------------- | ------------------------------- |
| Double Ctrl          | Start or stop English dictation |
| Double Ctrl + Option | Chinese dictation trigger       |
| Enter                | Stop active dictation           |
| Esc                  | Skip the current speech item    |
| Double Esc           | Cancel active playback          |
| Double Right CMD     | Start or pause voice chat       |

## Browser Authentication Security

- Firefox-synchronized cookies are equivalent to login credentials.
- Never print, upload, or commit cookie snapshots or bearer tokens.
- The Native Messaging host is restricted to the configured Firefox extension ID.
- The local authentication-sync socket uses user-only permissions.
- Generated Native Host files are stored under `~/.voice-edge`.
- Firefox-synchronized credentials take precedence over environment-provided browser credentials for the current process.

## Google AI Studio and Gemini App Relays

Voice Edge exposes two distinct Gemini relay endpoints under the `gemini:` namespace over `POST /v1/chat/completions`:

### 1. Google AI Studio Playground (`gemini:aistudio/*`)

- **Browser Extension Bridge**: Operates through the [voice-edge-auth-sync](https://github.com/woniuzfb/voice-edge-auth-sync) browser extension over the shared local WebSocket bridge (`ws://127.0.0.1:5003/ws`, `AIS_*` protocol), driving `MakerSuiteService` RPCs (`GenerateContent`, `ListModels`, `CreatePrompt`, `UpdatePrompt`, `ListPrompts`, `GenerateTitle`) inside an authenticated `https://aistudio.google.com/` tab.
- **Silent WAA Bootstrap & Signing**: Automatically intercepts the page's Web Application Attestation (`BotGuard`) service and computes 3-part `SAPISIDHASH` headers on the fly without consuming quota or polluting Playground history on startup.
- **Capabilities**:
  - Native client tool declarations (`body[6]`), multi-turn function calling, and automatic `thoughtSignature` preservation across tool turns
  - Configurable thinking levels via suffix (`-high`, `-medium`, `-low`)
  - Multimodal inputs (inline images, audio, PDFs, and YouTube links) and Google Search grounding citations
  - Automatic Playground history persistence (`prompts/<id>`) and generated session title synchronization
- **Model IDs**:
  - `gemini:aistudio/gemini-4-argon` (`-high`, `-medium`, `-low`)
  - `gemini:aistudio/gemini-3.8-flash-high`, `gemini:aistudio/gemini-3.8-flash-medium`, `gemini:aistudio/gemini-3.8-flash-low`
  - Any upstream model dynamically discovered via `ListModels` (`gemini:aistudio/<model-slug>`)

### 2. Gemini macOS App Task Mode (`gemini:app/*`)

- **Native Robin gRPC & ERT Relay**: Reuses local macOS `Gemini.app` credentials to drive `RobinService/ProcessQuery` and bidirectional `ExecuteRemoteTools` (ERT) streams for [Voice-Edge Harness (`veh`)](https://github.com/woniuzfb/voice-edge-harness).
- **Capabilities**:
  - Stateful multi-turn continuation, `StateTape` `state_seq` synchronization, and automatic `resumeTurn` stream rollover
  - Hot-reloadable multi-account switching, history divergence (`fork`) detection, and SQLite state persistence (`~/.voice-edge/gemini_dsh_state.sqlite3`)
- **Model IDs**:
  - `gemini:app/gemini-4-argon` (`-high`, `-medium`, `-low`)

## OpenRouter and OpenCode Relays

Both relays expose third-party models as local OpenAI-compatible models: model ids carry the `openrouter:` or `opencode:` prefix and clients only speak `POST /v1/chat/completions`, while upstream differences (Responses, Anthropic Messages, Google generateContent) are translated locally. Use `GET /v1/models` to list what is available; a relay without configured keys publishes nothing.

### OpenRouter

```bash
export VE_OPENROUTER_API_KEYS='["key-one","key-two"]'
```

- Model ids: `openrouter:<upstream-model-id>`.
- Keys share state inside the process: round-robin rotation.

### OpenCode

OpenCode reads exactly three environment variables:

```bash
export VE_OPENCODE_API_KEYS='["key-one","key-two"]'

# Default 1: publish only Zen models whose id ends in "-free"; set to 0 to publish every Zen model
export VE_OPENCODE_FREE=1

# Default 0: set to 1 to publish OpenCode Go models as well
export VE_OPENCODE_GO=0
```

- Model ids: `opencode:zen/<model-id>` and `opencode:go/<model-id>`.
- The key policy matches OpenRouter.

`"publish": True` always publishes the model regardless of `VE_OPENCODE_FREE` / `VE_OPENCODE_GO`, `"publish": False` always hides it, and an entry without the field follows those two environment variables.

## Architecture

```text
  Voice-Edge Harness (veh) / Continue (VS Code) / OpenAI Clients / MCP (:5001)
  macOS Global Hotkeys / Local Wake-Word Voice Chat / Xiaomi XiaoAI Speaker
                                      |
                                      v
                    +-----------------------------------+
                    |     Voice Edge Core (:5002)       |
                    |  OpenAI API Router & MCP Server   |
                    +-----------------+-----------------+
                                      |
        +-----------------------------+-----------------------------+
        |                             |                             |
        v                             v                             v
+---------------+           +-------------------+           +---------------+
| Local AI &    |           | Browser & Desktop |           | Cloud API     |
| Speech Engine |           | Model Relays      |           | Relays        |
+-------+-------+           +---------+---------+           +-------+-------+
| - MLX-LM/VLM  |           | - Camoufox Engine |           | - OpenRouter  |
| - Embeddings  |           |   DeepSeek/Doubao |           |   openrouter: |
|   & Rerank    |           |   Qwen            |           | - OpenCode    |
| - FIM         |           | - WS Bridge :5003 |           |   opencode:   |
| - Whisper /   |           |   M365 Copilot &  |           |   zen/ & go/  |
|   Apple STT   |           |   AI Studio (AIS) |           +---------------+
| - Edge-TTS    |           | - Gemini.app      |
+-------+-------+           |   Robin gRPC/ERT  |
        |                   +-------------------+
        v
+-----------------------------------+
| macOS HUD / Audio & XiaoAI Stream |
+-----------------------------------+
```

## Debugging

Enable only the diagnostics needed for the active provider:

```bash
export VE_XIAOAI_LOG_DEBUG=1
export VE_DOUBAO_LOG_DEBUG=1
export VE_QWEN_LOG_DEBUG=1
export VE_DEEPSEEK_LOG_DEBUG=1
export VE_DEEPSEEK_LOG_STREAM_CHUNKS=1
export VE_M365_LOG_DEBUG=1
export VE_M365_LOG_RELAY_TRACE=1
export VE_M365_LOG_ATTACHMENT=1
export VE_GEMINI_LOG_DEBUG=1
```

Do not share logs until secrets such as cookies, access tokens, refresh tokens, authorization headers, and temporary download credentials have been removed.

## License

MIT License.
