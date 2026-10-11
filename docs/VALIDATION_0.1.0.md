> Historical 0.1.0 material. For the current AgentGuard release see README.md, AGENTGUARD_HANDOFF.md and VALIDATION.md. Earlier GitHub publication was withdrawn.

# 0.1.0 验证记录

日期：2026-10-11。本次接手使用 Python 3.13.3；以下是离线与本地 HTTP 的软件验证结果，不是商业中转站检测数据。

| 项目 | 本次实测状态 |
|---|---|
| 原始 ZIP 的单元及本地 HTTP 集成测试 | 19/19 通过，修改前保存基线 |
| 必要修复后的完整测试 | 28/28 通过，新增 9 项回归 |
| 本地 Chat Completions / Anthropic Messages HTTP 传输 | 通过模拟服务验证 |
| 工具选择、调用次数、附带文本 | 合法响应中的失败计入能力评分分母；畸形协议包单列 |
| 密钥及 URL 回显 | 完整脱敏后截断元数据；长值及截断边界回归通过 |
| 非法 HTTP、截断响应、深层 JSON | 安全归类；不保存异常正文，保留已观察 HTTP 状态 |
| 禁止重定向 / 429 不重试 / 请求预算前置检查 | 测试通过 |
| 默认 CLI 演示 | 54 次模拟请求，`synthetic=true`，身份 `not_verified` |
| 演示与原包基线比较 | 完整 summary 相同；无指标回退 |
| 默认 CLI plan | 54 次；runner 凭证访问与 HTTP 打开拦截检查通过 |
| 参数不同的配对结果 | 标记条件不匹配，身份仍未验证 |
| 公开文件检查 | 未发现真实凭证；仅保留源码、文档、测试及明确模拟的示例 |
| 真实官方模型与中转站校准 | `NOT_RUN` |
| 模型身份误报率、漏报率 | `UNKNOWN` |
| 沙箱代码生成与执行 | 尚未实现 |
| GitHub 推送与远端 CI | 公开仓库已推送；Python 3.10 / 3.12 / 3.13 均通过，每个环境实际运行 28 项测试、演示和 plan |
| X 发布 | `NOT_RUN`，只交付可复制文案 |

## 演示基线

| 模拟端点 | 请求 | 可评分 | 通过 | 接口错误 |
|---|---:|---:|---:|---:|
| reference_demo | 27 | 27 | 27 | 0 |
| relay_demo | 27 | 26 | 13 | 1 |

两端标签同为 `claimed-model-demo`。26 对共同可评分题的差值为 -50 个百分点，解释为 `descriptive_only`。所有结果是 `synthetic_unverified`，模拟耗时由 Mock 预设，不能估计真实检测准确率、身份或推理速度。

## 复现命令

```bash
python3 -m unittest discover -s tests -v
python3 -m relay_audit demo --out reports/local-check-final-001
python3 -m relay_audit plan --config config.example.json
```

如目录已存在，请更换新的输出目录。详见 [EVALUATION.md](EVALUATION.md)；真实校准需在用户提供明确端点、环境变量配置及预算后单独开展。

## 远端验证

公开仓库：[zhouey314-cloud/relay-audit](https://github.com/zhouey314-cloud/relay-audit)。首个源码提交为 `bafa6130cf3da43b2a0f940f4b7d6a5be5f25973`；[该提交的 GitHub Actions](https://github.com/zhouey314-cloud/relay-audit/actions/runs/38104960804) 已完成，三个 Python 环境全部成功。运行日志分别记录 `Ran 28 tests`，并完成明确模拟的演示和 54 次请求计划。

已通过 GitHub API 逐文件核对全部 31 个提交文件的 blob SHA，与本地 Git 完全一致。本机 reports、配置、环境文件和本次接手的独立验证日志不在公开提交中。后续文档提交的 CI 结果可在 [Actions](https://github.com/zhouey314-cloud/relay-audit/actions) 查询；最终交付报告绑定实际最终 commit 与 CI。

源码发布门槛：`PASS`。真实模型身份校准：`NOT_RUN`；相应质量门槛尚未验收。两者不能混称。
