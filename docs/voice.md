# 可选语音链路

网页保留实时语音输入：连接反馈、暂停、继续、完成、取消、错误诊断，以及已输入文字的保留。语音不是图片生成的必需依赖。

## 本地实时识别

```bash
pip install -e '.[voice]'
export VOICE_STREAM_MODEL_DIR="/your/explicit/model-directory"
export VOICE_STREAM_AUTH_URL="http://127.0.0.1:8000/api/auth/me"
python -m media.voice_stream_server
```

需自行安装与 `sherpa_onnx.OnlineRecognizer.from_zipformer2_ctc` 兼容的模型文件，目录中包含 `model.int8.onnx` 和同级 `tokens.txt`；模型不随源码分发，不自动下载或搜索私人目录。默认只监听 `127.0.0.1:18766`。

浏览器通过 `/api/voice-stream` 连接，WebSocket 子协议为 `media-bearer`，第二个协商值携带当前登录 token；服务端仅回选协议名，不回显 token。令牌通过显式配置的 `/api/auth/me` 验证后才启动识别。不要在代理日志中记录 `Sec-WebSocket-Protocol`，也不要把 token 放入 URL。

开发时 Vite 已代理该路径。独立生产部署需要将这个路径代理至上述本机 WebSocket 服务，并启用 Upgrade/Connection 转发；页面必须使用 HTTPS（localhost 例外）才能使用麦克风。也可以在前端构建时通过 `VITE_VOICE_STREAM_WS` 设置 WebSocket 地址。

## 文字整理和音频文件接口

`/api/voice/draft-refine` 和 `/api/voice/draft-refine/stream` 沿用已有文本整理逻辑，使用显式配置的对话 provider。流式输出支持取消，手动编辑不会被迟到结果覆盖，未确认的建议可显式采用或撤销。

兼容的 `/api/voice/voice-draft` 音频上传接口使用 Groq Whisper，需要 `GROQ_API_KEY`；`GROQ_BASE_URL` 可显式配置。该接口与本地实时识别独立，当前生图页主要使用实时 WebSocket。音频转换依赖 `av`，包含在 `voice` extra 中。

本轮覆盖合成 token 校验、错误 token 不启动模型、协商不回显凭据、PCM/partial/final/done 流程、取消及迟到结果隔离。不声称已验证真实麦克风、下载的 ASR 模型或真实 Groq 转写。
