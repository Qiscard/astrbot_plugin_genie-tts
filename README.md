# AstrBot Genie-TTS (v2.3)

基于 Genie TTS Gateway 的 AstrBot 语音合成插件。

## v2.3 亮点

- **分类配置页**（借鉴 `astrbot_plugin_splitter` 的 `condition` 写法）
  - 简易模式：一页开关搞定自动 TTS / 情绪 / 分段 / 过滤 / 预热
  - 完整模式：`gateway` · `voices` · `emotion_detect` · `filter` · `split` · `warmup` · `trigger`
- **分段 TTS**：按句号等切分，前 n-1 段主动发送，最后一段交给框架；延迟模拟真人节奏
- **预热降级**：模型休眠预热期间按文本分段输出，热机后恢复语音
- **情绪识别**（参考 realistic-persona）：`keyword` / `llm` / `hybrid`
- **颜文字过滤**（参考 tts_sanitizer）
- **层叠音色**：`voices` 模板按角色配置情绪映射，首次同步写入配置

> 若同时启用本插件分段与 `astrbot_plugin_splitter`，可能双重分段，建议二选一。

## 快速开始

1. 填写 `base_url` + `api_key`
2. 选择「简易模式」或「完整模式」
3. 重载插件；首次会自动 sync 角色/情绪到 `voices`
4. 在完整模式微调各角色情绪路由后再次重载
5. `gentts test 你好啊~`

## 指令

| 指令 | 说明 |
|------|------|
| `gentts help` | 帮助 |
| `gentts test <文本>` | 试听 |
| `gentts filter <文本>` | 过滤预览 |
| `gentts split <文本>` | 分段预览 |
| `gentts emotion <文本>` | 情绪识别预览 |
| `gentts status` | 状态 |
| `gentts sync` / `sync force` | 同步模型到配置 |
| `gentts voices` | 查看层叠音色 |
| `gentts list` / `use` / `emo` | 角色与情绪 |
| `gentts on/off` | 会话开关 |
| `gentts globalon/globaloff` | 全局开关 |
| `gentts wake/sleep/unload/reload` | 模型热度 |

## 配置分类（完整模式）

- **gateway**：角色、语言、超时、重试
- **voices**：层叠音色模板（角色 → 情绪映射）
- **emotion_detect**：情绪识别模式与阈值
- **filter**：代码/表情/链接/Markdown/颜文字/替换词/静音裁剪
- **split**：是否分段、最大段数、分隔符、发送速度、逐段 TTS
- **warmup**：休眠预热与提示
- **trigger**：全局开关、概率、字数上限、冷却

## 依赖

见 `requirements.txt`（`aiohttp`，可选 `pydub` 做静音裁剪）。
