# RelayAudit

**给 LLM API 中转站做可复现的能力审计。报告异常证据，不出售“模型验真证书”。**

Local-first, zero runtime dependencies, reproducible probes. Capability evidence is not model identity proof.

当前版本：`0.1.0`。已经完成本机离线与本地 HTTP 集成测试；**尚未用真实官方 API / 中转站校准，也没有可靠模型身份指纹库**。

## 30 秒看演示

需要 Python 3.10+。进入项目根目录，无需安装第三方包，无需 API Key：

```bash
python3 -m relay_audit demo --out reports/my-first-demo
```

Windows 使用 `py -3` 替换 `python3`。打开生成的 `report.html`。同一模型标签下，模拟参考端点和模拟降级端点会出现不同答题表现。页面和 JSON 都明确标记 `synthetic=true`，它不代表真实站点测试结果。

示例报告见 [examples/demo/report.md](examples/demo/report.md) 与 [examples/demo/report.html](examples/demo/report.html)。

## 能测什么

| 探针 | 当前实际实现 | 解释边界 |
|---|---|---|
| 推理 | 模运算、流水筛选的本地标准答案 | 只反映这些题上的表现 |
| 格式与指令 | 严格 JSON、额外字段、Unicode 精确复制 | 网关、采样和模型都可能影响结果 |
| 代码追踪 | 给定 Python 代码，判定返回值 | **不等于代码生成或工程编程能力** |
| 上下文检索 | 约 1K、4K、16K 字符噪声，首/中/尾锚点 | 不是 128K/1M token 窗口认证；失败不独立证明截断 |
| 工具调用 | 强制调用函数并核验函数名及参数 | 400/限流/协议错误单列，不当成答题降智 |
| 两端对照 | 同种子题目，交替请求，按题配对 | 比较的是用户指定参考端点，不自动保证参考真实性 |
| 工程信号 | HTTP 错误、完整响应耗时、未验证 model/usage 字段 | 不把速度或字段自报当成型号证据 |

每个种子变体生成 9 道独立请求题。默认 3 个变体、2 个端点，共 54 次计划请求。新变体改变数据，**不等于完全隐藏评测结构**；公开题型仍可被识别。

## 测自己的端点

1. 将 `config.example.json` 复制成 `config.local.json`。
2. 填写每个端点的**完整 HTTP 请求 URL**、协议、模型快照或映射别名，以及保存 Key 的**环境变量名称**。配置文件不接受明文 Key。
3. 在本机设置这些环境变量，值不需要发给开发者或提交仓库。
4. 先看请求计划，再执行：

```bash
python3 -m relay_audit plan --config config.local.json
python3 -m relay_audit run --config config.local.json --out reports/real-test-001
```

`plan` 不读 Key、不发请求；`run` 才调用端点。不自动重试，不自动换模型，不增加备用端点。最多允许 8 个端点，其中最多 1 个参考端点。预算不足会在发请求之前停止。

只测一个中转也可以，删除参考端点即可，但只能描述自身能力与错误，不能据此判定掉包。

协议支持 `chat_completions` 和 `anthropic_messages`。原生 Gemini、OpenAI Responses、流式 SSE、多模态尚未实现。不要把不同协议、快照、系统提示词、推理预算或采样条件的结果当作同条件比较。

`limit_field` 对 chat 接口可选 `max_tokens` / `max_completion_tokens`；选端点支持的字段。Anthropic 接口始终使用 `max_tokens`。可选 `parameters` 仅支持 `temperature`、`top_p`、`seed`、`reasoning_effort`、`thinking`；并非每个模型支持这些参数，不做静默删参。思考模型可能先用完输出/推理预算，因此要结合 `finish_reason` 和自己的配置解释失败。

目前直接连接指定端点，不使用环境中的 HTTP 代理。原生 Anthropic 示例见 `examples/anthropic-config.json`。无需工具调用测试时，可在配置顶层设置 `families`，比如 `["modular", "code_trace", "context_4096"]`；报告覆盖范围随之缩小。

## 预算、数据与隐私

- 运行工具本身不用部署服务器；真实 API 请求按端点规则计费。
- 请求数量上限可控。输入字符数和请求输出上限**不是实际 token 数或人民币支出硬上限**。还要考虑思考、缓存、输入计费与服务商规则。
- `max_wall_seconds` 控制后续请求是否发起；socket timeout 与 DNS/网络行为不能保证精确的整个进程截止时间。
- API Key 仅从指定环境变量读取，不落盘、不发往开发者。它会作为认证信息发送给用户配置的端点，这是测试所必需的。
- 禁止跨 URL 重定向。默认输出不含 URL、认证头或答案原文，只保存计分、哈希和允许的响应字段。
- `--save-responses` 可在本地保存经过已知密钥精确替换的答案文本，用于复核。它不是通用敏感信息检测器，分享前应检查。
- 报告和本机配置默认被 Git 忽略。输出目录必须是新目录，防止覆盖原报告。无遥测、无第三方 judge 调用。

生成：`manifest.json`、逐请求 `results.jsonl`、`summary.json`、Markdown 与离线 HTML 报告。`usage` 为服务商自报，未独立校验账单。

## 怎么读结果

回答可以评分时：记录 `passed=true/false`。网络、HTTP、无法解析的响应包等：`passed=null`，单列错误。**答题通过率与 API 成功率要一起看**，成功应答的题目子集可能存在选择偏差。

有参考端点时，只对两边都能评分的同一道题算配对通过率差，标记 `descriptive_only`。没有参考、没有配对样本或声明的协议/参数不同，则为 `inconclusive_conditions_or_samples`。`declared_conditions_match` 只比较客户端声明条件，无法观察服务商隐藏系统提示或路由。

所有报告的模型身份结论都是 `not_verified`。当前版本没有校准误报率，不给 p 值、“掺水百分比”、认证总分或“100% 真模型”。两端成绩一致只意味着**当前样本未观察到明显差异**。成绩不同还可能由版本、网关加工、思考预算、队列或格式兼容造成。

题目较小，强模型可能全部满分。需要识别具体型号时，应另行使用同协议、同快照的指纹基线并校准；不要将 ChatGPT 网页回答当 API 基线。

## 复现与测试

```bash
python3 -m unittest discover -s tests -v
python3 -m relay_audit probes
python3 -m relay_audit export-probes --seed 20261011 --variants 3
```

导出的题目、标准答案与摘要可供独立检查。默认报告中的响应哈希无法还原原文；需要重新计算历史评分时，应当事先启用本地答案保存。

原始交付包的 19 项测试通过；发布前补充 9 项回归，当前共 28 项测试。覆盖预算、报告、密钥回显脱敏、严格计分、工具行为失败、异常 HTTP/JSON、参数差异、本地 HTTP 的两种协议、禁止重定向、不重试 429，以及无凭证无网络的请求计划。本地测试通过不代表 GitHub Actions 已运行，也不代表真实商业检测准确度。实际运行记录见 [docs/VALIDATION.md](docs/VALIDATION.md)，基线与回归契约见 [docs/EVALUATION.md](docs/EVALUATION.md)。

## 下一版

先用官方同快照端点作阴性对照，再用已知不同模型/关闭思考/受控截断作阳性对照；报告误报、漏报、成本和参数敏感性。之后再加入沙箱代码生成测试、SSE、真实 token 计数与账单对照、长窗口分档、多时段持续采样和经校准的行为指纹。

参见 [docs/METHODOLOGY.md](docs/METHODOLOGY.md)、[docs/CODEX_HANDOFF.md](docs/CODEX_HANDOFF.md) 和 [docs/X_DRAFT.md](docs/X_DRAFT.md)。

## 相关工作

这个问题已有研究与项目，不声称首创或“全网唯一”：

- [Real Money, Fake Models](https://arxiv.org/abs/2603.01919)：第三方 shadow API 审计研究。
- [fpverify](https://github.com/Mohamed7415/fpverify)：基于行为分布与参考指纹的检测，需注意条件匹配及其说明的局限。
- [apicheck](https://github.com/shipinghai/apicheck)：另一类 API 检测项目。
- [KBF](https://arxiv.org/abs/2605.29524)：知识边界指纹与黑盒审计方法。

上述是背景与后续调研入口。当前源码为独立实现，没有复制这些项目的代码、题库或指纹数据。

MIT License.
