# Voice Edge AI

Voice Edge AI is a macOS-focused local voice and AI platform. It combines local MLX language and vision models, faster-whisper/apple speech recognition, streaming Edge-TTS synthesis, OpenAI-compatible HTTP APIs, MCP tools, native macOS dictation, Xiaomi XiaoAI integration, and browser-backed AI providers.

## Highlights

- Local MLX language and vision inference
- Real-time and file-based speech recognition with faster-whisper
- Streaming speech synthesis with Edge-TTS
- OpenAI-compatible chat, transcription, speech, embedding, rerank, and FIM endpoints
- MCP Streamable HTTP tools
- Native macOS dictation, HUD, keyboard shortcuts, and audio-route recovery
- Xiaomi XiaoAI smart-speaker integration
- Browser-backed DeepSeek, Doubao, Qwen, and Microsoft 365 Copilot models
- Firefox-assisted local authentication synchronization
- Optional SharePoint uploads for Microsoft 365 Copilot attachments

Voice Edge AI can be used as a local voice assistant, inference gateway, MCP server, transcription service, streaming TTS gateway, or Xiaomi XiaoAI backend.

## Requirements

- macOS
- Python 3.11 or newer
- FFmpeg
- Microphone permission
- Accessibility permission
- Input Monitoring permission

Apple Silicon is recommended for MLX-based inference.

## Installation

```bash
brew install ffmpeg
uv sync
uv run python -m camoufox fetch
```

Install the included Firefox authentication-sync extension when using browser-backed providers.

Install the included Continue extension package when using Voice Edge through VS Code.

## Running

### MCP mode

```bash
uv run start
```

### HTTP mode

```bash
uv run start --http
```

Default ports:

- MCP: `5001`
- HTTP: `5000`

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

Firefox synchronizes local browser authentication state through the Native Messaging bridge. Authentication values are treated as secrets and must not be logged, uploaded, or committed.

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
export XIAOAI_MODEL='LLM:m365-chatgpt-5.6'
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
export XIAOAI_NEW_CONVERSATION='new conversation,new chat,clear context,change topic,start over'
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

## Keyboard Shortcuts

| Shortcut             | Action                          |
| -------------------- | ------------------------------- |
| Double Ctrl          | Start or stop English dictation |
| Double Ctrl + Option | Chinese dictation trigger       |
| Enter                | Stop active dictation           |
| Esc                  | Skip the current speech item    |
| Double Esc           | Cancel active playback          |

## Browser Authentication Security

- Firefox-synchronized cookies are equivalent to login credentials.
- Never print, upload, or commit cookie snapshots or bearer tokens.
- The Native Messaging host is restricted to the configured Firefox extension ID.
- The local authentication-sync socket uses user-only permissions.
- Generated Native Host files are stored under `~/.voice-edge`.
- Firefox-synchronized credentials take precedence over environment-provided browser credentials for the current process.

## Architecture

```text
   XiaoAI / OpenAI / MCP / Hotkeys
                |
                v
        Voice Edge AI Core
                |
     +----------+----------+
     |          |          |
     v          v          v
    STT      Local LLM    Tools
     |          |          |
     +----------+----------+
                |
                v
      Conversation Engine
                |
                v
          Streaming TTS
                |
                v
   Local Audio / XiaoAI Audio
```

## Debugging

Enable only the diagnostics needed for the active provider:

```bash
export DOUBAO_DEBUG=1
export QWEN_DEBUG=1
export DEEPSEEK_DEBUG=1
export DEEPSEEK_LOG_STREAM_CHUNKS=1
export M365_DEBUG=1
export M365_RELAY_TRACE=1
export M365_ATTACHMENT_DEBUG=1
export XIAOAI_POLL_DEBUG=1
export XIAOAI_PLAYER_STATUS_DEBUG=1
```

Do not share logs until secrets such as cookies, access tokens, refresh tokens, authorization headers, and temporary download credentials have been removed.

## License

MIT License.
