# AstrBot Genie-TTS 插件 (v2.1 Gateway)

对接 **Genie TTS Gateway**（`/api/v1/*` + `X-API-Key`），把 LLM 回复转为语音。

## 新特性（v2.1）

### 1. 预热模式（默认开启）
模型休眠/未加载时：
1. **本轮按文本输出**（不阻塞用户）
2. **后台自动预热**（`/api/v1/wake` + 短文本冷加载）
3. **预热完成后**，后续对话恢复 TTS

可在配置分组 `warmup` 中开关，并可选附加“正在预热”提示。

### 2. 模型列表加载 / 情绪自动选择
- 启动时拉取角色与情绪目录
- `emotion_id=0` 且情绪名为空时，自动绑定该角色默认情绪
- 切换角色时自动加载默认情绪（可关）

命令：
```text
gentts list
gentts characters
gentts emotions [角色]
gentts use <角色名|序号> [情绪名|ID|序号]
gentts char <角色名|序号>
gentts emo <情绪名|ID|序号>
```

### 3. 文本处理与裁剪合并
配置里归入分组 **`text_process`**：
- 过滤代码 / Emoji / URL / Markdown
- 静音裁剪
- 是否替换原文、是否语音附带文本

## 配置分组

| 分组/项 | 说明 |
|---|---|
| `base_url` / `api_key` | Gateway 与鉴权 |
| `character` / `emotion_id` / `emotion` / `language` | 默认音色 |
| `model_select.auto_select_emotion` | 切角色自动选默认情绪 |
| `warmup.enabled` | 休眠时文本降级+后台预热 |
| `warmup.show_tip` | 文本后附加预热提示 |
| `text_process.*` | 过滤与裁剪 |
| `prob` / `text_limit` / `cooldown` | 触发控制 |

## 常用指令

```text
gentts help
gentts status
gentts test 你好
gentts list
gentts use 1
gentts use lxh 标准
gentts emo 4
gentts on / off
```

管理员：`gentts wake` / `sleep` / `unload` / `reload` / `globalon` / `globaloff`

## 推荐流程

1. 填 `base_url` + `api_key`
2. `gentts list` 看角色情绪
3. `gentts use lxh` 或 `gentts use 1`
4. 若状态为休眠，先聊天（文本），预热完自动变语音
5. `gentts test 你好，今天天气不错。` 验证

## 行为说明

- 自动 TTS 仅处理 LLM 回复
- 预热中的自动回复 **不会改成语音**，保留原文
- `gentts test` 仍会直接请求合成（可用于强制预热/试听）
- 空闲约 20 分钟网关会休眠；插件下次自动走预热降级

## 版本

- v2.1.0 预热模式、模型/情绪选择、配置分组
- v2.0.0 适配 Gateway API
