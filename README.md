# Media Workspace

一个可以自行部署的 AI 生图网页：先把想法聊成方案，按需绑定参考图，明确确认后生成；结果可以下载、回看，并接着上一张图继续修改。

沿用 React + FastAPI + SQLite，包含完整前后端。它与 [image-mcp](https://github.com/bmdy6/image-mcp) 面向同一个创作方向：这里提供网页交互，image-mcp 提供 Agent 工具入口。两者独立运行，不共享线上后端；本仓不提供托管演示服务。

## 可以做什么

- 对话流与方案卡：区分讨论和实际生图，确认前不会提交图片请求。
- 参考图上传、方案卡绑定和移除；上传进度、取消、失败后重试保留。
- 持久化图片任务、同请求幂等、刷新恢复、历史会话和继续修改。
- 用户密码登录、邀请码注册、JWT 认证、用户间数据隔离。
- 可选实时语音输入、录音暂停/继续/取消，以及保留原文的文字整理。

新参考图上传需要配置腾讯云 COS。默认本地模式可以文生图，并在本地原图有效期内继续修改；默认原图保留 24 小时，请及时下载。历史记录不等于永久保留原图。需要长期图片存储时启用 COS 转存，详见 [配置与部署](docs/configuration.md)。

## 本地安装

需要 Python 3.10+、Node.js 20.19+ 或 22.12+，运行平台为 Linux/macOS（图片任务的进程锁使用 `fcntl`）。

```bash
git clone https://github.com/bmdy6/media-workspace.git
cd media-workspace
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[test]'
npm --prefix frontend ci
npm --prefix frontend run build
```

将 [.env.example](.env.example) 中需要的变量显式导出到启动进程。应用不会自动加载 `.env`，也不会查找宿主机的密钥仓库、工作台账号或其他项目配置。至少设置：

```bash
export MEDIA_DATA_DIR="$PWD/data"
export SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
export MEDIA_CHAT_BASE_URL="https://api.openai.com/v1"
export MEDIA_IMAGE_BASE_URL="https://api.openai.com/v1"
# 从自己的安全配置中注入 MEDIA_CHAT_API_KEY 和 MEDIA_IMAGE_API_KEY；勿提交到 Git。
python -m media.cli admin create admin
uvicorn media.web.backend.main:app --host 127.0.0.1 --port 8000 --workers 1
```

创建管理员时会隐藏输入密码。打开 `http://127.0.0.1:8000`，使用该账号登录。`SECRET_KEY` 需至少 32 字符；正式部署应生成一次并持久保存，重启不要更换。新密码至少 6 字符、最多 72 个 UTF-8 字节。`python -m media.cli invite create` 可以创建一次性邀请码。

默认对话模型 `gpt-6-luna`、图片模型 `gpt-image-2`，使用官方 Chat Completions / Images 接口；默认图片为 1:1、low。模型权限以自己的账号为准。修改模型或使用 OhMyGPT 时，需要按 [配置说明](docs/configuration.md) 同时配置模型、参数和协议，不只是替换 URL。

## 不调用模型的体验与开发

```bash
# 先按上面完成安装和前端构建
python scripts/synthetic_demo.py
```

打开 `http://127.0.0.1:8765`，使用 `demo` / `synthetic-demo-password`。此入口只监听本机，使用临时 SQLite、合成图片和内存 COS 替身，阻断外部 HTTP 请求，退出会清理临时数据。它用于检查流程，不代表真实模型效果，也不能作为生产启动入口。

前端开发可在另一个终端运行 `npm --prefix frontend run dev`；Vite 将 `/api` 转发至本机 8000，将语音 WebSocket 转发至本机 18766。语音配置与边界见 [语音说明](docs/voice.md)。

## 验证

```bash
pytest -q
npm --prefix frontend test
npm --prefix frontend run build
```

测试使用合成数据。已有独立真实验收覆盖：官方方案请求、low 文生图、通过本地历史图继续编辑、受保护原图下载和重复确认幂等；参考图的真实 COS 上传和真实麦克风/ASR 尚未验证。完整证据与限制见 [验证记录](docs/validation.md)。

本项目从已有个人创作工作区提取。来源修订、文件哈希和独立化边界见 [来源说明](docs/provenance.md) 与 [SOURCE_MANIFEST.json](SOURCE_MANIFEST.json)。MIT License。
