# AstrBot Genie-TTS (v2.2)

Genie TTS Gateway 客户端插件。

## v2.2.1 情绪识别增强
参考 [astrbot-plugin-realistic-persona](https://github.com/LMG-arch/astrbot-plugin-realistic-persona)：
- 标准情绪枚举：开心/悲伤/生气/兴奋/平静/困惑/无聊/好奇/惊讶/焦虑
- **hybrid**：关键词优先 + LLM 兜底
- 会话情绪平滑，减少音色乱跳
- gentts emotion <文本> 预览识别与映射

## v2.2 新功能
1. **颜文字过滤**（参考 [tts_sanitizer](https://github.com/Luna-channel/astrbot_plugin_tts_sanitizer)）
2. **LLM 情绪识别** → 按音色映射自动切换参考音
3. **层叠音色配置** oices：模型 → 情绪感知映射
4. **首次启动自动同步** Gateway 角色/情绪到配置文件

## 层叠配置示例
`	ext
voices
└─ lxh
   ├ default_emotion: 标准
   └ emotion_routes
      ├ 高兴 -> id=4 标准
      ├ 悲伤 -> id=2 ...
      └ 平静 -> ...
`

首次连接后自动写入；也可 gentts sync / gentts sync force。

## 指令
- gentts test/status/list/use/emo
- gentts sync 同步模型到配置
- gentts voices 查看映射
- gentts filter <文本> 过滤预览
- gentts wake/sleep/reload

## 配置分组
- oices 层叠音色
- emotion_detect LLM 情绪识别
- 	ext_process 过滤/颜文字/裁剪
- warmup 休眠预热文本降级
- model_select 自动同步

## 建议流程
1. 配置 base_url + api_key
2. 重启插件，等待首次 sync
3. 仪表盘微调 voices 情绪映射
4. 再重载一次
5. gentts test 你好呀~ (笑)
