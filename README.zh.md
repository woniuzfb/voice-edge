# Voice Edge

Voice Edge 是一个面向 macOS 的本地语音与 AI 平台，集成本地 MLX 语言与视觉模型、faster-whisper/apple 语音识别、Edge-TTS 流式语音合成、OpenAI 兼容 HTTP API、MCP 工具、macOS 原生听写、小米小爱同学集成，以及基于浏览器的 AI 模型。

## 主要能力

- 本地 MLX 语言与视觉推理
- 基于 faster-whisper 的实时识别和文件转写
- Edge-TTS 流式语音合成
- OpenAI 兼容的聊天、转写、语音、嵌入、重排与 FIM 接口
- MCP Streamable HTTP 工具
- macOS 原生听写、HUD、全局快捷键与音频路由恢复
- 小米小爱音箱集成
- 基于浏览器的 DeepSeek、豆包、Qwen 和 Microsoft 365 Copilot 模型
- Firefox 辅助的本地认证同步
- 面向 Microsoft 365 Copilot 附件的可选 SharePoint 上传能力

Voice Edge 可作为本地语音助手、推理网关、MCP 服务器、转写服务、流式 TTS 网关或小爱同学后端使用。

## 系统要求

- macOS
- Python 3.11 或更高版本
- FFmpeg
- 麦克风权限
- 辅助功能权限
- 输入监控权限

使用 MLX 推理时推荐 Apple Silicon。

## 安装

```bash
brew install ffmpeg
uv sync
uv run python -m camoufox fetch
```

使用浏览器模型时，请安装项目随附的 Firefox 认证同步插件。

通过 VS Code 使用 Voice Edge 时，请安装项目随附的 Continue 插件。

## 运行

### MCP 模式

```bash
uv run start
```

### HTTP 模式

```bash
uv run start --http
```

默认端口：

- MCP：`5001`
- HTTP：`5000`

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

Firefox 通过 Native Messaging 桥接同步本地浏览器认证状态。认证内容属于敏感凭据，不得写入日志、上传或提交到代码仓库。

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
export XIAOAI_MODEL='LLM:m365-chatgpt-5.6'
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
VE_VOICE_CHAT_KEYWORDS = (
    ("你好丁丁", None, None),
    ("你好包子", None, "0.10"),
    ("换个话题", None, None),
)

VE_VOICE_CHAT_WAKE_ROUTES = {
    "你好丁丁": ("model", "LLM:deepseek"),
    "你好包子": ("model", "LLM:doubao"),
    "换个话题": ("command", "new_session"),
}

VE_VOICE_CHAT_MODEL_ALIAS = {
    "LLM:deepseek": os.getenv("VE_VOICE_CHAT_ALIAS_DEEPSEEK", "DeepSeek"),
    "LLM:doubao": os.getenv("VE_VOICE_CHAT_ALIAS_DOUBAO", "豆包"),
}
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
    apiBase: http://localhost:5000/v1/
    roles:
      - chat
    capabilities:
      - tool_use
  - name: LLM:m365-claude-opus
    provider: Local
    model: LLM:m365-claude-opus
    apiBase: http://localhost:5000/v1/
    roles:
      - chat
    defaultCompletionOptions:
      contextLength: 1000000
      maxTokens: 128000
    capabilities:
      - tool_use
      - image_input
    excludeToolOutputsFromTokenCount: true
  - name: LLM:m365-chatgpt-5.6
    provider: Local
    model: LLM:m365-chatgpt-5.6
    apiBase: http://localhost:5000/v1/
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
    apiBase: http://localhost:5000/v1/
    roles:
      - embed
  - name: rerank:jina-v3
    provider: Local
    model: embed:jina-v3
    apiBase: http://localhost:5000/v1/
    roles:
      - rerank
  - name: FIM:qwen-2.5-coder-1.5B
    provider: Local
    model: FIM:qwen-2.5-coder-1.5B
    apiBase: http://localhost:5000/v1/
    roles:
      - edit
      - apply
      - autocomplete
  - name: FIM:qwen-2.5-coder-7B
    provider: Local
    model: FIM:qwen-2.5-coder-7B
    apiBase: http://localhost:5000/v1/
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
| 双击右 CMD         | 开始或停止 聊天      |

## 浏览器认证安全

- Firefox 同步的 Cookie 等同于登录凭据。
- 不要打印、上传或提交 Cookie 快照及 Bearer Token。
- Native Messaging Host 仅允许配置的 Firefox 扩展 ID 访问。
- 本地认证同步 Socket 使用仅限当前用户的权限。
- 生成的 Native Host 文件存放在 `~/.voice-edge`。
- 对于当前进程，Firefox 同步的凭据优先于环境变量提供的浏览器凭据。

## 架构

```text
     小爱同学 / OpenAI / MCP / 快捷键
                  |
                  v
          Voice Edge 核心
                  |
       +----------+----------+
       |          |          |
       v          v          v
      STT       本地 LLM     工具
       |          |          |
       +----------+----------+
                  |
                  v
              对话引擎
                  |
                  v
             流式 TTS
                  |
                  v
          本地音频 / 小爱音频
```

## 调试

仅启用当前提供商所需的诊断开关：

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

分享日志前，必须移除 Cookie、访问令牌、刷新令牌、Authorization Header 和临时下载凭据等敏感信息。

## 许可证

MIT License。
