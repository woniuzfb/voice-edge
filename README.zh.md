# Voice Edge - [A]LL [I]n One Script

## 结合

- **本地 Apple Silicon AI**：`mlx-lm` 与 `mlx-vlm` 本地推理、嵌入（Embeddings）、重排（Rerank）以及 FIM 代码补全
- **语音与交互引擎**：实时语音转写（`faster-whisper` / Apple Speech）、流式 Edge-TTS 语音合成、macOS 全局听写、原生 HUD 浮层与本地唤醒词语音助手
- **统一 OpenAI 与 MCP 网关**：标准 OpenAI 兼容 HTTP 接口（`/v1/chat/completions`、`/v1/audio/*`、`/v1/embeddings`、`/v1/rerank`、`/v1/completions`）与 MCP 服务
- **浏览器模型中继**：配合 [voice-edge-auth-sync](https://github.com/woniuzfb/voice-edge-auth-sync) Firefox 扩展接入 DeepSeek、豆包（Doubao）、Qwen、Microsoft 365 Copilot 以及 Google AI Studio Playground（`gemini:aistudio/*`）
- **Gemini App 任务模式中继**：复用 macOS 原生 `Gemini.app` 接入 Argon 模型（`gemini:app/*`），通过 Robin gRPC 与 `ExecuteRemoteTools`（ERT）双向流驱动 [Voice-Edge Harness (veh)](https://github.com/woniuzfb/voice-edge-harness)
- **第三方 API 中继**：OpenRouter（`openrouter:*`）与 OpenCode（`opencode:zen/*`、`opencode:go/*`），支持多 Key 轮询与上游协议自动转换
- **小米小爱同学桥接**：局域网智能音箱桥接，支持唤醒词路由、流式 TTS 播报与小米原生助手兜底

## 系统要求

- macOS（Apple Silicon）
- Python 3.11 或更高版本
- FFmpeg
- 麦克风权限
- 辅助功能权限
- 输入监控权限
- [voice-edge-auth-sync](https://github.com/woniuzfb/voice-edge-auth-sync)
- [continue](https://github.com/woniuzfb/continue)
- [voice edge harness](https://github.com/woniuzfb/voice-edge-harness)

## 安装

```bash
brew install ffmpeg
uv sync
uv run python -m camoufox fetch
```

使用浏览器模型（DeepSeek、豆包、Qwen、Microsoft 365 Copilot 或 Google AI Studio Playground）时，请安装上面的 Firefox 认证同步插件。

通过 VS Code 使用 Voice Edge 时，请安装上面的 Continue 插件。

使用 Gemini App Argon 模型（`gemini:app/*`）或 AI Studio Playground 模型（`gemini:aistudio/*`）进行 Agent 任务编排时，请安装上面的 Voice-Edge Harness（`veh`）。

## 运行

### MCP 模式

```bash
uv run main
```

### HTTP 模式

```bash
uv run main --http
```

默认端口：

- MCP：`5001`
- HTTP：`5002`

首次启动时，本地组件初始化可能需要更长时间。

## OpenAI 兼容 API

| 能力       | 接口                            |
| ---------- | ------------------------------- |
| 聊天补全   | `POST /v1/chat/completions`     |
| 语音转文字 | `POST /v1/audio/transcriptions` |
| 文字转语音 | `POST /v1/audio/speech`         |
| 嵌入       | `POST /v1/embeddings`           |
| 重排       | `POST /v1/rerank`               |
| FIM        | `POST /v1/completions`          |

## 核心功能

### 语音识别

- 实时语音转写
- 音频与视频文件转写
- 全局听写模式
- Apple Speech 集成
- 多语言识别

### 本地 AI

- MLX-LM 和 MLX-VLM 后端
- 本地嵌入与重排
- FIM 代码补全
- OpenAI 兼容的模型路由

### 语音合成

- Edge-TTS 流式播放
- 多种语音别名
- 串行音频队列
- 长文本语音支持
- 自动播放恢复

### macOS 集成

- 原生 HUD 浮层
- 全局键盘快捷键
- 持久化音频输出流
- 蓝牙及音频路由恢复

### 浏览器模型

支持的浏览器模型包括：

- DeepSeek：快速、专家与识图模式
- 豆包
- Qwen
- Microsoft 365 Copilot
- Google AI Studio Playground（`gemini:aistudio/*`）

Firefox 通过 Native Messaging 桥接与本地 WebSocket 通道（`ws://127.0.0.1:5003/ws`）同步本地浏览器认证状态与 RPC 调用。认证内容属于敏感凭据，不得写入日志、上传或提交到代码仓库。

## Microsoft 365 Copilot 与 SharePoint

配置 Microsoft 365 入口页面和可选的 SharePoint 上传和下载位置：

```bash
export M365_ENTRY_URL='https://outlook.cloud.microsoft/host/xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx/entity1-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx'
export SHAREPOINT_HOME_URL='https://tenant.sharepoint.com/sites/site_name'

# 传文件给模型时存放的位置
export SHAREPOINT_UPLOAD_FOLDER='Upload'

# 模型给你文件时存放的位置
export SHAREPOINT_DOWNLOAD_FOLDER='Download'
```

`SHAREPOINT_UPLOAD_FOLDER` 是相对于文档库的路径。留空时使用文档库根目录。

`SHAREPOINT_DOWNLOAD_FOLDER` 是相对于文档库的路径。留空时使用`SHAREPOINT_UPLOAD_FOLDER`。

对话状态配置：

```bash
# 自动恢复本地保存的最后一次浏览器对话。
export VOICE_EDGE_RESTORE_LAST_CONVERSATION=1

# 禁止在本地持久化对话状态。
export VOICE_EDGE_DISABLE_CONVERSATION_PERSISTENCE=1

# 浏览器模型统一状态文件。
export VOICE_EDGE_BROWSER_STATE_PATH="$HOME/.voice-edge/browser-provider-state.json"
```

当支持的客户端向 Microsoft 365 模型上传文件时，Voice Edge 可以先将文件上传到配置的 SharePoint 目录，再将其作为文件附件发送给 Microsoft 365 Copilot。

## 小米小爱同学配置

启用小米桥接并设置账号及设备参数：

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

选择 AI 模型：

```bash
# 豆包
export XIAOAI_MODEL='LLM:doubao'
export DOUBAO_BROWSER_ENGINE='camoufox'

# Qwen
export XIAOAI_MODEL='LLM:qwen'
export QWEN_BROWSER_MODEL='qwen3.7-plus'

# DeepSeek
export XIAOAI_MODEL='LLM:deepseek'

# Microsoft 365 Copilot 示例
export XIAOAI_MODEL='LLM:m365-chatgpt-6.1'
```

系统提示词仅用于支持 system 消息角色的本地模型，不会自动添加到浏览器模型的用户消息前。

```bash
export XIAOAI_SYSTEM_PROMPT='请简洁回答，只输出适合语音朗读的文字。'
export XIAOAI_MAX_TOKENS=500
export XIAOAI_TEMPERATURE=0.3
```

### 模型路由

```bash
export XIAOAI_TRIGGER_WITHOUT_KEYWORD=1
export XIAOAI_KEYWORDS='帮我,请'
export XIAOAI_STOP_PHRASES='停止回答,停止,停一下'
export XIAOAI_NEW_CONVERSATION='新建对话,新对话,新会话,清空上下文,换个话题,重新开始'
```

匹配以下关键词的请求会绕过 AI 模型，交由小米原生助手处理：

```bash
export XIAOAI_NATIVE_KEYWORDS='天气,时间,几点钟'
export XIAOAI_NATIVE_STATUS_POLL_INTERVAL=0.25
export XIAOAI_NATIVE_PLAY_START_TIMEOUT=3.0
export XIAOAI_NATIVE_PLAY_END_TIMEOUT=30.0
export XIAOAI_NATIVE_IDLE_CONFIRMATIONS=1
export XIAOAI_NATIVE_STATUS_FALLBACK_DELAY=3.0
export XIAOAI_NATIVE_TAIL_GUARD=0.20
```

### 播放与音频流

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

小米设备会通过局域网拉取音频流，因此公开地址不要使用 `127.0.0.1`。

### 流式处理与轮询

```bash
export XIAOAI_POLL_INTERVAL=0.1
export XIAOAI_POLL_MIN_INTERVAL=0.08
export XIAOAI_POLL_DEBUG=0
export XIAOAI_POLL_LOG_EVERY=1
export XIAOAI_AUDIO_MAX_BUFFER_BYTES=524288
export XIAOAI_SPEECH_TARGET_CHARS=42
export XIAOAI_HISTORY_TURNS=6
export XIAOAI_MP3_BITRATE='64k'
export XIAOAI_QUERY_DEBOUNCE_SECONDS=4
export XIAOAI_WAKEUP_SUPPRESS_SECONDS=0
```

### 可选 Tavily 搜索

```bash
export TAVILY_API_KEY='your_api_key'
export XIAOAI_TAVILY_TOOL_ENABLED=1
export XIAOAI_TAVILY_TOOL_MAX_RESULTS=3
export XIAOAI_TAVILY_TOOL_TIMEOUT=30
```

### 可选 本地 miIO

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

## 本机语音助手

```bash
export VE_VOICE_CHAT_ENABLED=1
export VE_VOICE_CHAT_MODEL_DIR='/path/to/sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20'
export VE_VOICE_CHAT_ALIAS_DEEPSEEK='DeepSeek'
export VE_VOICE_CHAT_ALIAS_DOUBAO='豆包'
export VE_VOICE_CHAT_HISTORY_CHAR_BUDGET=6000
export VE_VOICE_CHAT_SHOW_HUD=0
export VE_VOICE_CHAT_KWS_ENGINE='apple'
export SPEECH_HELPER_ON_DEVICE=1
export VE_VOICE_CHAT_CAPTURE_LOCALES='zh-CN,en-US'
```

```python
# 唤醒配置唯一真值源。每一项只定义一个正式 keyword；aliases 仅属于该 keyword。
# command_locale=None 表示后续命令自动采用第一个返回有效文本的 locale。
VE_VOICE_CHAT_WAKE_CONFIG = (
    {
        "keyword": "你好丁丁",
        "aliases": ("你好钉钉", "你好丁丁丁", "你好deepseek"),
        "route": ("model", "LLM:deepseek"),
        "command_locale": "zh-CN",
        "model_alias": os.getenv("VE_VOICE_CHAT_ALIAS_DEEPSEEK", "DeepSeek"),
        "score": None,
        "threshold": None,
        "apple_threshold": 0.84,
    },
    {
        "keyword": "你好豆包",
        "aliases": ("你好包子",),
        "route": ("model", "LLM:doubao"),
        "command_locale": "zh-CN",
        "model_alias": os.getenv("VE_VOICE_CHAT_ALIAS_DOUBAO", "豆包"),
        "score": None,
        "threshold": None,
        "apple_threshold": 0.84,
    },
    {
        "keyword": "换个话题",
        "aliases": (),
        "route": ("command", "new_session"),
        "command_locale": "zh-CN",
        "model_alias": None,
        "score": None,
        "threshold": None,
        "apple_threshold": 0.84,
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

## 键盘快捷键

| 快捷键             | 操作                 |
| ------------------ | -------------------- |
| 双击 Ctrl          | 开始或停止 英文 听写 |
| 双击 Ctrl + Option | 中文听写触发方式     |
| Enter              | 停止当前听写         |
| Esc                | 跳过当前语音项       |
| 双击 Esc           | 取消当前播放         |
| 双击右 CMD         | 开始或暂停 聊天      |

## 浏览器认证安全

- Firefox 同步的 Cookie 等同于登录凭据。
- 不要打印、上传或提交 Cookie 快照及 Bearer Token。
- Native Messaging Host 仅允许配置的 Firefox 扩展 ID 访问。
- 本地认证同步 Socket 使用仅限当前用户的权限。
- 生成的 Native Host 文件存放在 `~/.voice-edge`。
- 对于当前进程，Firefox 同步的凭据优先于环境变量提供的浏览器凭据。

## Google AI Studio 与 Gemini App 中继

Voice Edge 在 `gemini:` 命名空间下通过 `POST /v1/chat/completions` 暴露两条独立的 Gemini 中继通道：

### 1. Google AI Studio Playground（`gemini:aistudio/*`）

- **浏览器扩展桥接**：通过 [voice-edge-auth-sync](https://github.com/woniuzfb/voice-edge-auth-sync) 浏览器扩展与本地共享 WebSocket 桥（`ws://127.0.0.1:5003/ws`，`AIS_*` 协议），在已登录的 `https://aistudio.google.com/` 标签页内驱动 `MakerSuiteService` RPC（`GenerateContent`、`ListModels`、`CreatePrompt`、`UpdatePrompt`、`ListPrompts`、`GenerateTitle`）。
- **静默 WAA 自举与请求签名**：在标签页内自动捕获 Web Application Attestation（`BotGuard`）服务并实时计算三段 `SAPISIDHASH` 认证头，冷启动自举全程零配额消耗、零历史记录污染。
- **核心能力**：
  - 原生客户端工具声明（`body[6]`）、多轮函数调用，以及跨工具轮次自动保存与回填 `thoughtSignature`
  - 通过模型后缀（`-high`、`-medium`、`-low`）切换思考强度（Thinking Level）
  - 多模态输入（内联图片、音频、PDF、YouTube 链接）与 Google Search 联网引用（Grounding Citations）
  - 自动持久化 Playground 会话历史（`prompts/<id>`）并同步生成标题
- **模型 ID**：
  - `gemini:aistudio/gemini-4-argon`（`-high`、`-medium`、`-low`）
  - `gemini:aistudio/gemini-3.8-flash-high`、`gemini:aistudio/gemini-3.8-flash-medium`、`gemini:aistudio/gemini-3.8-flash-low`
  - 任意由 `ListModels` 动态发现的上游模型（`gemini:aistudio/<model-slug>`）

### 2. Gemini macOS App 任务模式（`gemini:app/*`）

- **原生 Robin gRPC 与 ERT 双向流中继**：复用本机 macOS `Gemini.app` 登录凭据，通过 `RobinService/ProcessQuery` 与 `ExecuteRemoteTools`（ERT）双向流驱动 [Voice-Edge Harness（`veh`）](https://github.com/woniuzfb/voice-edge-harness)。
- **核心能力**：
  - 有状态多轮会话续接、`StateTape` `state_seq` 同步以及长连接 `resumeTurn` 自动流轮转（Rollover）
  - 多账号凭据指纹热切换、历史回滚/分叉（`fork`）检测，以及 SQLite 状态库持久化（`~/.voice-edge/gemini_dsh_state.sqlite3`）
- **模型 ID**：
  - `gemini:app/gemini-4-argon`（`-high`、`-medium`、`-low`）

## OpenRouter 与 OpenCode 中继

两个中继都把第三方模型暴露为本地 OpenAI 兼容模型：模型 ID 带各自的 `openrouter:` / `opencode:` 前缀，客户端只用 `/v1/chat/completions`，上游的 Responses、Anthropic Messages、Google generateContent 等协议差异由本地转换。可用模型通过 `GET /v1/models` 查询，未配置 key 的中继不会发布任何模型。

### OpenRouter

```bash
export VE_OPENROUTER_API_KEYS='["key-one","key-two"]'
```

- 模型 ID：`openrouter:<上游模型ID>`
- 多 key 在进程内共享状态：round-robin 轮询。

### OpenCode

```bash
export VE_OPENCODE_API_KEYS='["key-one","key-two"]'

# 默认 1：只发布 zen 里以 "-free" 结尾的免费模型；设为 0 后发布全部 zen 模型
export VE_OPENCODE_FREE=1

# 默认 0：设为 1 后再发布 go 模型
export VE_OPENCODE_GO=0
```

- 模型 ID：`opencode:zen/<模型ID>`、`opencode:go/<模型ID>`。
- 多 key 策略与 OpenRouter 相同。

`"publish": True` 无视 `VE_OPENCODE_FREE` / `VE_OPENCODE_GO` 始终发布，`"publish": False` 始终隐藏，不写则按上述两个环境变量判断。

## 架构

```text
  Voice-Edge Harness (veh) / Continue (VS Code) / OpenAI 客户端 / MCP (:5001)
  macOS 全局快捷键 / 本地唤醒词语音助手 / 小米小爱同学智能音箱
                                      |
                                      v
                    +-----------------------------------+
                    |     Voice Edge 核心 (:5002)       |
                    |   OpenAI API 路由与 MCP 服务端    |
                    +-----------------+-----------------+
                                      |
        +-----------------------------+-----------------------------+
        |                             |                             |
        v                             v                             v
+---------------+           +-------------------+           +---------------+
| 本地 AI 与    |           | 浏览器与桌面端    |           | 云端 API      |
| 语音引擎      |           | 模型中继          |           | 中继          |
+-------+-------+           +---------+---------+           +-------+-------+
| - MLX-LM/VLM  |           | - Camoufox 引擎   |           | - OpenRouter  |
| - Embeddings  |           |   DeepSeek / 豆包 |           |   openrouter: |
|   & Rerank    |           |   Qwen            |           | - OpenCode    |
| - FIM 补全    |           | - WS 桥接 :5003   |           |   opencode:   |
| - Whisper /   |           |   M365 Copilot &  |           |   zen/ & go/  |
|   Apple STT   |           |   AI Studio (AIS) |           +---------------+
| - Edge-TTS    |           | - Gemini.app      |
+-------+-------+           |   Robin gRPC/ERT  |
        |                   +-------------------+
        v
+-----------------------------------+
| macOS HUD / 本地音频 & 小爱音频流 |
+-----------------------------------+
```

## 调试

仅启用当前提供商所需的诊断开关：

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

分享日志前，必须移除 Cookie、访问令牌、刷新令牌、Authorization Header 和临时下载凭据等敏感信息。

## 许可证

MIT License。
