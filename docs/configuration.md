# 配置与部署

## Provider 与协议

默认 `media/config/runtime.json` 使用官方 `gpt-6-luna` Chat Completions 和 `gpt-image-2` Images 接口。`MEDIA_CHAT_BASE_URL`、`MEDIA_IMAGE_BASE_URL` 均填写 `https://api.openai.com/v1`，对应的 Key 由进程环境注入。应用不读取宿主机 vault、不交换工作台身份、不附加私有 relay header；不会继承历史转发密钥。

默认目录中的 `verified=false` 表示不能从源码目录推断你的账号有权限。运行时目录是协议与能力配置，不是对所有列出的模型提供可用性保证。非默认模型需要自行启用、配置对应 provider 的环境变量并验证。

若明确使用 OhMyGPT，将 `MEDIA_RUNTIME_CONFIG` 指向 `media/config/runtime-ohmygpt.json`，并把 `MEDIA_IMAGE_BASE_URL` 设为服务商对应的 API 前缀。该配置使用现有 `ohmygpt_images_polling` 协议：提交 `omg.async_method=pulling`，保存 `task_id` 与同源 `pull_url` 后轮询；不能拿它直接请求官方 API。两个协议均有独立合成参数回归。

## 存储

默认 `defaults.storage_policy=direct`。网页任务保留本地原图，经身份校验的 `/api/image/result/{task_id}` 返回；默认保留 86400 秒，可在 `image_jobs.result_retention_seconds` 配置。该原图有效期间，可通过归属于当前用户的 `parent_id` 继续修改。超时后不能依赖本地原图恢复，应该提前下载。

上述默认针对网页使用的托管任务接口。兼容的同步 `/api/image/generate` 和 CLI `test-gen` 入口仍使用 `defaults.synchronous_storage_policy=cos`，需要 COS；它们不会把不支持的存储策略伪装成已成功处理。

新上传参考图依赖腾讯云 COS 的四项环境变量，见模板。它沿用原有 COS 路径，不引入另一套本地上传存储。要长期转存生成作品，将运行时配置的 `defaults.storage_policy` 改为 `background_cos`（先提供结果，后台转存）或 `cos`（转存流程）。对象 URL 必须能被需要读取参考图的模型服务访问；请自行确认桶策略和可分享内容。COS 转存失败会保留尚未过期的本地结果并显示失败状态，不伪称永久保存成功。

`MEDIA_DATA_DIR` 包含 SQLite 数据库和任务结果；正式部署必须挂持久卷。默认 SQLite。可选 MySQL 接缝保留，但未做本轮 MySQL 实例验收；设置 `MEDIA_USE_MYSQL=1` 和 `DATABASE_URL` 才会尝试，原逻辑连接失败会降级到 SQLite，不能将它当作跨实例一致性的保证。

## 运行

运行单个 Uvicorn worker。图片队列使用本地进程锁和 SQLite 任务状态，另一个 worker 不能同时接管队列。多副本部署需要另行设计，不在当前交付范围。

前端可由 FastAPI 托管：先 `npm --prefix frontend run build`。若单独托管前端，构建时设置 `VITE_MEDIA_API_BASE` 指向真实后端前缀；后端 `MEDIA_CORS_ORIGINS` 填写明确允许的页面 origin，以逗号分隔，不使用通配 origin。网页 API 使用 Bearer token，所有运行密钥只能留在后端。

公网部署时使用自己的 HTTPS 反向代理，只将代理指向本机后端。使用语音需要额外代理 WebSocket，详见 [语音说明](voice.md)。`/health` 只反映进程及队列状态，不证明模型、COS 或语音已可用。默认不提供公开注册，只有有效一次性邀请码可以创建普通账号。

备份前停止应用，复制整个 `MEDIA_DATA_DIR`，另行妥善保管 `SECRET_KEY` 和自己的部署配置。恢复时先验证备份，使用相同密钥启动单个进程。不要把数据目录、生成图片、账号或运行凭据提交到仓库。
