# RelayAudit 0.1.0 — 初版

给 LLM API 中转站做可复现的能力审计，报告能力差异与异常证据。**真实模型身份校准尚未完成；本版本不是模型身份认证器。**

## 使用

需要 Python 3.10+，运行时没有第三方依赖。进入仓库根目录：

```bash
python3 -m relay_audit demo --out reports/my-first-demo
python3 -m relay_audit plan --config config.example.json
python3 -m unittest discover -s tests -v
```

Windows 使用 `py -3`。演示不用 Key，也不请求外部模型；输出目录必须不存在。默认请求计划为 2 个端点 × 27 道题 = 54 次请求。`plan` 不读凭证、不发请求，只有 `run` 会调用用户配置的端点；真实 API 费用由用户选择的服务商收取，输出 token 上限不是金额硬上限。

## 当前 CLI 范围

- `demo`、`plan`、`run`、`probes`、`export-probes`。
- 推理、严格 JSON、Unicode、代码追踪、约 1K/4K/16K 字符上下文检索与强制工具调用。
- 支持 Chat Completions、原生 Anthropic Messages 的非流式文本接口。
- 生成 manifest、逐请求 JSONL、summary、Markdown 和离线 HTML；错误与答题评分分开，按题配对比较仅作描述。

尚未实现 Responses、原生 Gemini、SSE、多模态、沙箱代码生成执行或真实身份指纹。

## 验证与修复

原始交付包 19/19 本地测试通过；发布前新增 9 项回归，当前 28/28 通过。验证仅涉及 Mock 和本地 HTTP。54 次模拟演示的 summary 与原包基线完全一致，页面及数据明确标记模拟，身份结论保持 `not_verified`。

修复合法响应中的工具行为失败被排除评分的问题、先截断后脱敏可能留下密钥/URL 前缀的问题，以及异常 HTTP/深层 JSON 的安全分类。API 错误仍不计为能力失败。保留 MIT License 和相关工作归属。

远端工作流实际结果见 [验证记录](VALIDATION.md) 与仓库 Actions。测试或 CI 通过仅证明当前软件契约，**不证明真实模型检测准确率、误报率、漏报率或服务商意图**。无付费 API 测试；真实官方模型与中转站校准为 `NOT_RUN`。

参见 [README](../README.md)、[方法边界](METHODOLOGY.md)、[回归契约](EVALUATION.md)。
