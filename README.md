# RelayAudit · AgentGuard

**接入 Agent 请求链路，持续监测并自动告警。**

Agent → 本地网关 → 用户指定的中转 API。正常聊天、写代码、工具调用继续进行；网关被动观察模型声明、协议完整性和服务变化，自动发出独立提醒与本地证据报告。

当前版本 **0.2.1**。默认 `passive`，**额外模型请求为零**。**真实模型身份校准尚未完成：标签和全部可见行为相同的隐藏换模无法验证；本项目不承诺识别所有替换，也没有经过校准的准确率。**

## 稳定演示与下载

- [在线查看纯合成报告](examples/guard-synthetic/report.md)：受版本控制，标记 `synthetic_unverified`，模型身份始终未验证。
- [v0.2.1 安装包、合成演示与 SHA256](https://github.com/zhouey314-cloud/relay-audit/releases/tag/v0.2.1)：下载 wheel 后在独立环境 `python -m pip install ./relay_audit_lite-0.2.1-py3-none-any.whl`，运行 `relay-audit guard demo --out demo-001`。
- [发行范围与验证方式](docs/RELEASE_NOTES_0.2.1.md)：重复 Connection 头过滤、监测降级期间的真实传输结果保留；没有修改模型检测规则或宣称真实模型校准通过。

Actions 工件继续作为每次 CI 的测试证据，并有保留期限；上面的版本报告和 Release 下载是长期演示入口。旧 v0.2.0 标签和资产保留。

## 无真实 Key 的演示

Python 3.10+；使用独立虚拟环境：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.lock
python -m pip install --no-deps --no-build-isolation -e .
relay-audit guard demo --out agentguard-demo/run-001
```

演示启动本机模拟 Agent 与模拟中转：正常调用 → 标签改变 → 自动终端告警和 Markdown 报告 → 第三次调用继续完成。目录必须新建。所有模拟素材与期望结果标为 `synthetic_unverified`，不代表识别过真实中转站。

[五分钟 QuickStart 与可下载纯合成 fixture](examples/guard-synthetic/README.md)
提供完整命令和 ZIP 白名单。GitHub Actions 每个 Python 版本提供
`synthetic-demo-python-*` 下载工件，内含 summary、report、请求样例和 SHA256
清单，不包含运行状态、association key 或真实素材。

## 接入自己的 API 路线

真实端点、模型和预算必须由使用者明确配置。此命令只生成配置，不请求模型：

```bash
relay-audit guard init --config guard.local.json \
  --upstream https://your-relay.example/v1 --model your-model --port 18765
```

在网关进程环境设置 `RELAY_GUARD_TOKEN`（随机本地访问令牌）和 `RELAY_UPSTREAM_KEY`（自己的中转 API Key），然后：

```bash
relay-audit guard doctor --config guard.local.json
relay-audit guard start --config guard.local.json
```

`doctor` 默认只检查配置、本机端口和可用客户端，不请求收费 API。`doctor --mock` 验证另外建立的本机模拟流式链路。前台网关的 stderr 自动显示告警；可用 `start --background`，终端告警写到私有 state 目录的 `gateway-terminal.log`。可选 macOS 系统通知通过 `notifications.system=true` 启用，弹窗显示取决于操作系统权限。

把 Agent 的 Base URL 指向 `http://127.0.0.1:18765/v1`，其 API 凭证使用 **本地令牌**。网关将它替换为固定上游 Key，不转发 Cookie 或订阅登录认证。绑定仅允许 loopback，不接受任意转发目标、不跟随重定向、不自动重试或换模型。

Codex 用隔离目录，生成器拒绝修改正在使用的 `~/.codex`：

```bash
relay-audit guard attach-codex --config guard.local.json \
  --codex-home ./codex-guard-home --model your-model
python ./codex-guard-home/relayguard-launch.py --skip-git-repo-check "你的任务"
```

启动器使用当前本机版本实测的显式 provider 配置。Claude Code 有 `attach-claude` 和 Stop hook；具体 API 认证隔离、启动与恢复见 [Agent 接入指南](docs/AGENT_INTEGRATION.md)。本次没有将任何真实账户流量切换到网关。

## 已验收范围

| 范围 | 当前证据 |
| --- | --- |
| Responses / Chat Completions / Anthropic Messages | JSON 与 SSE 实体字节保真、Unicode 分块、增量输出、多轮工具、usage/cache、结束形态 |
| 模型声明 | 中途标签变化即时告警；允许映射与主动换模型不误报；缺字段不可观测 |
| 服务变化 | 按会话、模型、协议、推理/输出/工具/输入/缓存条件分组；最少 20 个基线样本 + 20 个当前样本，描述性阈值可配置 |
| 异常与恢复 | 429、超时、取消、输出预算、协议错误分开；事件去重、冷却、恢复记录 |
| 故障隔离 | 慢解析/检测/通知、存储失败和队列溢出继续转发，并显示监测降级 |
| Codex CLI | 本机真实客户端 + 模拟 Responses SSE；notify 回调执行，外部终端提醒；未在客户端聊天输出看到告警 |
| Claude Code | 本机真实客户端 + 模拟 Messages SSE；Stop hook 提醒在客户端 hook_response 可见 |
| 生命周期 | 真实子进程启动、状态、停止、重启；detach 校验摘要，恢复原路径；拒绝覆盖后来修改的配置 |

真实客户端的模型端点都是模拟服务，Key 都是假的。上述验收证明接入与通知机制，不证明真实模型身份或商业效果。[测试与演示记录](docs/VALIDATION.md)、[协议范围](docs/PROTOCOLS.md)、[重复验收契约](evals/PROJECT_EVAL_SPEC.md)给出证据和未完成项。

## 读懂告警与基线

- `declared_model_mismatch`：**声明模型不一致**，只证明标签不属于配置的允许映射。
- `protocol_or_capability_error`：可观察的协议/能力错误；无法确定是模型、网关还是参数兼容问题。
- `service_degradation`：可比窗口的错误率或耗时变化，包含样本数及前后分布；没有统计校准的置信度。
- `behavioral_drift_suspected`：明确启用复核、匹配可信参考且参考自身校准通过后，合成探针支持的疑似行为变化。
- `monitoring_degraded`：观察丢失、监测模块或通知故障；不能据此判断上游正常。

日常基线只代表过去观测到的表现。样本不足显示 **数据不足**；所有状态保持 `identity_status=unverified`。异常样本不立即吸收进学习基线。使用者可在 `model_mappings` 配置允许的别名/快照；没有映射时按请求模型的精确标签比较。提供 `X-Session-Id` 等稳定会话标识时使用本地 HMAC 关联；缺少会话标识则逐请求独立，宁可数据不足也不混合会话。

## 可选后台复核

只有 `mode=verified_probe`、`probe.enabled=true`、明确 target、正数 hour/day 请求预算都满足时才发起独立合成请求。默认配置关闭它。参考与目标需同协议、同声明快照和请求条件；参考先自校准。没有参考或条件不匹配显示 `uncalibrated`，不宣称身份识别。

复用旧版本地标准答案探针，独立队列/会话，无 judge API，不复制用户对话。预算和事件冷却落盘并原子保留到重启后；耗尽停止复核，继续被动观察。请求数硬上限**不是金额硬上限**。[复核配置示例](examples/guard-probe-config.json)默认仍关闭；只有自行完成明确配置后才能启用。

## 隐私、管理与退出

不保存密钥、认证头、Cookie、提示词、代码、回答、推理或工具参数正文。只保留白名单元数据、受限外部标签和密钥化会话摘要；usage 为**上游自报**。事件和报告写在本机；目录权限 700、文件 600。保留期限默认 14 天，启动时清理，也可手动执行：

```bash
relay-audit guard status --config guard.local.json
relay-audit guard report --config guard.local.json --out report-001.md
relay-audit guard cleanup --config guard.local.json
relay-audit guard backup --config guard.local.json --out guard-backup.json
relay-audit guard restore --backup guard-backup.json --out guard-restored.json
relay-audit guard stop --config guard.local.json
relay-audit guard detach --manifest ./codex-guard-home/.relayguard-adapter/codex-manifest.json
```

备份不包含 Key，restore 只创建新文件；detach 在安装文件与备份摘要都未变时恢复。结束使用网关的 Agent 后选择原配置重新启动。**网关进程退出会中断连接，不会神奇地直连**；支持重新启动及安全恢复配置，尚未提供操作系统进程监督安装器。

当前实现限制：原生 WebSocket 明确返回 426；Codex 隔离配置使用官方支持的 HTTP/SSE。压缩实体保持转发，但压缩内容不解码观测，明确标为降级。32 MiB 请求实体上限；默认单个观测事件/JSON 64 KiB、解析队列及检测队列各 256 项，超限继续转发、减少证据。未实现原生 Gemini、多模态语义评测、MCP 服务、所有 IDE/WorkBuddy 接入及真实模型身份指纹库。详细边界见 [隐私与检测边界](docs/PRIVACY_AND_LIMITS.md)。

## 复现

```bash
python -m unittest discover -s tests -v
python evals/run_acceptance.py --out acceptance-local --requests 100
# 同样的纯合成负载，8 并发；任何 HTTP/实体错误会使验收失败：
python evals/run_acceptance.py --out acceptance-concurrent --requests 200 --concurrency 8
# 显式选择本机 Codex / Claude Code 的模拟端点接入验收：
python evals/run_acceptance.py --out acceptance-with-clients --requests 100 --clients
```

验收只使用本机模拟服务，不读历史聊天 Key，不改变订阅登录。CI 不依赖这两个客户端，也不会调用收费模型。测量直连、仅代理和开启观测的相同合成负载，报告真实开销，**不承诺零延迟**。

旧手动探针 CLI 继续可用，说明归档在 [旧版探针](docs/LEGACY_PROBES.md)；它是复核/调试功能。项目 MIT 许可，基于现有 RelayAudit 代码继续开发，未采用第三方身份指纹算法或复制它们的准确率。X 文案见 [docs/X_DRAFT.md](docs/X_DRAFT.md)，仅用于手动复制。
