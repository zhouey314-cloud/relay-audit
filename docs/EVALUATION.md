# 初版验证契约与回归依据

RelayAudit 0.1.0 的发布目标是可复现的能力记录、严格计分和安全的本地报告流程。源码发布不以真实模型身份校准完成为前提，也不代表校准通过。

## 原包基线

2026-10-11，在 Python 3.13.3 上运行原包的 `python3 -m unittest discover -s tests -v`：19/19 通过。原始 ZIP 的 SHA256 为 `aa2537a3bc5cd2a3f2353d173ec2a3cf15d4c6a93b42d3e4cef114e53489f419`。

`synthetic-v1` 提供 9 类题型、每类 3 个种子变体。默认演示产生 54 条模拟记录：参考端点 27/27 通过；模拟降级端点 13/26 通过，另有 1 次模拟 HTTP 错误；26 对共同可评分题的差值为 -50 个百分点。220/90 ms 是 Mock 的预设值，不是实测模型延迟。

题目和 Mock 均标记为 `synthetic_unverified`：本地确定性答案可以检查生成与计分机制，但不是经过真实模型与人类业务验收的 Golden Set。当前没有真实模型身份指纹基线、误报率、漏报率或生产失败数据。

## 发布前发现与必要修复

回归约束来自题目指令、SECURITY.md 和报告的错误/计分契约，不来自另一个模型的裁判意见。

| 失败 | 分类 | 必须满足的约束 |
|---|---|---|
| 合法响应中调用错误函数、缺失或多次调用 | Tool Selection / Instruction Following | 计为能力失败；保留在评分及配对分母内 |
| 工具调用同时附带普通回答文本 | Instruction Following | 不得通过要求“只调用工具”的题目 |
| 未能解析返回包 | Parsing | 与能力失败区分，保留已观察到的 HTTP 状态 |
| 元数据截断使已知密钥/URL 只剩前缀 | Workflow / Business Rule | 先做已知值精确替换，再截断保存字段 |
| 非法 HTTP 状态行或不完整响应抛出异常 | Tool Failure / Business Rule | 分类并记录；不输出异常正文及凭证回显 |
| 深层 JSON 使解析/计分递归溢出 | Parsing | 安全归类为无效包或无效答案，不中断整个运行 |

这些回归使用假凭证、确定性响应和 loopback 服务。它们验证软件对已知行为的处理，不衡量真实服务商的检测准确率。

## 发布门槛

1. 原有测试和新增回归全部通过；不得删除困难用例或放宽题目要求。
2. 新旧 Mock 演示的记录数、可评分数、通过数和配对差一致；所有演示持续明确标记模拟，身份结论为 `not_verified`。
3. 默认 `plan` 为 54 次；验证时拦截 runner 的凭证访问和 HTTP 打开，二者均不得发生。
4. 提交中不得包含本机配置、环境文件、真实报告或真实凭证。已知值替换仍不是通用敏感数据识别。
5. GitHub Actions 的 Python 3.10、3.12、3.13 测试矩阵实际通过。工作流文件本身不算 CI 证据。

最终结果与运行链接记录在 [VALIDATION.md](VALIDATION.md)。真实 API 校准为 `NOT_RUN`；身份质量、误报率、漏报率、真实延迟和实际 API 成本为 `UNKNOWN`，不能作为已通过指标。

## 复现

```bash
python3 -m unittest discover -s tests -v
python3 -m relay_audit demo --out reports/my-new-verification
python3 -m relay_audit plan --config config.example.json
```

输出目录必须不存在。真实模型校准需要用户明确提供测试端点、环境变量配置和预算后另行执行。
