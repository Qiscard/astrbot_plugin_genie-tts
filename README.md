# AstrBot Genie-TTS (v2.4)

基于 Genie TTS Gateway 的 AstrBot 语音合成插件。

## v2.4 亮点

- **功能分类配置页**（借鉴 splitter 的 condition）
  - 基础配置 · 音色模型 · 情绪感知 · 过滤处理 · 分段发送 · 预热触发
  - 取消简易/完整模式，按功能模块直接管理
- **模糊对接**：角色名/情绪标签/语言/速度等输入自动规范化与别名匹配
- **分段 TTS**、**预热降级**、**hybrid 情绪识别**、**颜文字过滤**、**层叠音色**

> 若同时启用本插件分段与 strbot_plugin_splitter，可能双重分段，建议二选一。

## 快速开始

1. 填写 ase_url + pi_key
2. 在「配置分类」中切换各功能模块进行设置
3. 重载插件；首次会自动 sync 角色/情绪到 oices
4. 在「音色模型」微调情绪路由后再次重载
5. gentts test 你好啊~

## 指令

| 指令 | 说明 |
|------|------|
| gentts help | 帮助 |
| gentts test <文本> | 试听 |
| gentts filter <文本> | 过滤预览 |
| gentts split <文本> | 分段预览 |
| gentts emotion <文本> | 情绪识别预览 |
| gentts status | 状态 |
| gentts sync / sync force | 同步模型到配置 |
| gentts voices | 查看层叠音色 |
| gentts list / use / emo | 角色与情绪（支持模糊名） |
| gentts on/off | 会话开关 |
| gentts globalon/globaloff | 全局开关 |
| gentts wake/sleep/unload/reload | 模型热度 |

## 配置分类

- **基础配置**：角色、语言、超时、自动 TTS、概率、冷却
- **音色模型**：层叠 voices + 自动同步
- **情绪感知**：keyword / llm / hybrid
- **过滤处理**：代码/表情/链接/Markdown/颜文字/替换/静音裁剪
- **分段发送**：分隔符、最大段数、发送速度、逐段 TTS
- **预热触发**：休眠预热文本降级

## 模糊对接示例

- 角色：LXH / lxh / LxH → 同一模型
- 情绪：高兴 / 开心 / happy → 路由到对应音色
- 速度：ast / 快速；语言：中文 / zh；模式：混合 / hybrid
