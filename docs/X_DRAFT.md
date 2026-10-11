# X 文案（手动复制；未发布到 X）

RelayAudit 新增 AgentGuard 模式：把 Agent 的模型请求接到本地网关，在正常使用过程中持续观察，出现声明模型不一致、协议错误或可比窗口的服务变化时，自动提醒并生成本地证据报告。

默认被动监测，额外模型调用为零；不往回答、SSE 或工具参数里塞警告。已验收 Responses、Chat Completions、Anthropic Messages 的 JSON/SSE，以及真实 Codex / Claude Code 客户端连接模拟中转的通知链路。后台复核需手动启用并设置请求预算。

真实模型身份校准尚未完成。标签和可见行为完全相同的隐藏换模无法验证，不承诺识别所有替换，也没有编造准确率。

源码、模拟演示与测试证据：
https://github.com/zhouey314-cloud/relay-audit
