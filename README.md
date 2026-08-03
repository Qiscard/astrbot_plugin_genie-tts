# AstrBot Genie TTS Plugin

AstrBot 的 Genie TTS Gateway 插件，提供角色与情绪自动同步、情绪识别、逐句 TTS、模型预热降级以及会话级开关。

## 当前分段策略

- 只按完整句末标点切分：`。！？!?….`
- 短句不合并，不再受 `max_segments` 或 `min_segment_length` 限制。
- 每个完整句子单独调用一次 `/api/v1/speak`，按顺序等待，不并发生成。
- 插件负责切分后会向 Gateway 传递 `split_sentence=false`，避免二次切分。
- 无句末标点的长文本作为完整段保留，不从句中间硬截断。

## 安装

1. 将本目录复制到 AstrBot 的 `data/plugins/astrbot_plugin_genie_tts`。
2. 按 `_conf_schema.json` 配置 Gateway 地址、API Key、角色和情绪策略。
3. 可复制 `config.example.json` 作为配置参考；不要把真实 API Key 提交到仓库。
4. 重载插件后执行 `gentts test`、`gentts sync` 和 `gentts status`。

## 自动同步

插件启动后会从 Gateway 自动获取角色、参考音色和情绪列表，并更新 AstrBot 配置中的角色模板。手动维护的 `emotion_routes` 只用于语义标签到服务端情绪 ID 的映射，不需要重复填写服务端已经提供的信息。

## 主要命令

- `gentts test`：检查连通性、模型和音色。
- `gentts sync`：从 Gateway 自动同步角色与情绪。
- `gentts voices`：查看角色和 `emotion_routes` 映射。
- `gentts wake` / `gentts sleep`：预热或休眠服务端模型。
- `gentts reload`：重新读取插件配置。

完整配置、情绪路由和接口说明见 [PLUGIN_GUIDE.md](./PLUGIN_GUIDE.md)。

## 致谢

- TTS 情绪路由参考：[muyouzhi6/astrbot_plugin_tts_emotion_router](https://github.com/muyouzhi6/astrbot_plugin_tts_emotion_router)
- 分段过滤参考：[nuomicici/astrbot_plugin_splitter](https://github.com/nuomicici/astrbot_plugin_splitter)
