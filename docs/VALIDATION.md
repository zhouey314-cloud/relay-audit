# AgentGuard 0.2.0 验收记录

2026-10-11，本机 Python 3.13.3 / Darwin 27.0.0 / arm64，aiohttp 3.13.5。运行环境使用 requirements.lock 的固定依赖与独立 venv。真实 API、中转兼容性与模型身份校准均 **NOT_RUN**。所有 Mock 和期望结果为 **synthetic_unverified**。

正式命令：

```bash
python evals/run_acceptance.py --out <new-directory> --requests 100 --clients
```

## 实际结果

- **113 项测试通过，0 failures / 0 errors / 0 skipped，完整验收 gate 用时 31.668 秒；发布前最终源码复跑同样 113 项通过，用时 39.375 秒。** 旧 28 项基线全部保留通过，新增 85 项测试；没有已知回归。原基线不存在 AgentGuard，状态 NOT_IMPLEMENTED。
- 模拟连续链路：3 次 Agent 请求对应 3 次上游请求；自动产生 1 个标签事件及终端提醒，自动生成事件 JSON / Markdown，hook 读取 1 个新 incident；第三次请求继续完成，额外探针 0，队列丢弃 0。
- 六组相同负载性能基准：660 次上游请求 = 6 × (10 warmup + 100 measured)；每组 N=N、额外探针 0、队列丢失 0。
- 真实 Codex CLI `0.162.0-alpha.17.2`：模拟 Responses SSE，1 次上游请求，exit 0，响应标记收到，1 次 notify 回调 / 1 个新 incident，detach 成功。notify stderr **未在客户端捕获输出中出现**；确认的是外部回调执行及独立网关终端告警，不是聊天框推送。
- 真实 Claude Code `2.1.226`：模拟 Messages SSE，1 次上游请求，exit 0，响应标记收到，1 次 Stop hook / 1 个新 incident，提醒在客户端 `hook_response` 可见，detach 成功。
- macOS 系统通知接口实际提交成功：1 delivered / 0 failures，真实 API 请求 0；用户肉眼看到弹窗仍为 NOT_VERIFIED。
- 当前执行任务的 `~/.codex/config.toml` 验收前后 SHA-256 一致；未修改当前 Codex provider、订阅登录或 auth 文件。真实 API 流量尚未启用。

公开摘要在 [evals/results](../evals/results)。原始客户端输出、配置 home、关联密钥与测试私有状态不进入仓库。

## 15 个场景对应证据

| 场景 | 自动化证据 |
| --- | --- |
| 1. JSON / SSE 与直连实体一致 | test_guard_proxy.py 三协议精确字节比较、固定响应实体 |
| 2. UTF-8 / JSON 任意网络分块 | test_guard_observer.py 逐拆分位置、逐 byte CRLF、多行 data |
| 3. 多轮工具 / 碎片 / cache / 终止 | observer、proxy、adapters Harness 三协议用例 |
| 4. 中途标签切换自动可见告警 | proxy held 上游未结束时真实 Detector+Notifier sink 已收到告警；响应不含警告 |
| 5. 合法别名 / 主动换模 | detector 允许映射、模型独立分组、missing label 不判假 |
| 6. 同标签受控行为变化 | probe 三协议匹配参考 + 自校准 + 多样本差异，仅疑似行为变化 |
| 7. 全部可见行为相同 | detector / probe equivalence 用例保持 identity unverified，无 drift |
| 8. 429 / 超时 / 取消 / 输出预算 | proxy、hardening、observer 的独立分类；不重试 |
| 9. 慢/故障监测与队列溢出 | parser thread gate、detector slow/fail、storage/notifier 故障、两类 queue overflow；实体继续 |
| 10. passive N=N | proxy 与 660 次 benchmark 请求守恒；probe 配置未启用时请求 0 |
| 11. probe 预算/并发/冷却/耗尽 | persisted budget/trigger 冷却、跨 Store OS lock、立即恢复显示预算、读取失败 failclosed |
| 12. 会话/模型/条件不串台 | detector session/model/reasoning/cache/stream 分组与并发 request IDs |
| 13. 隐私 | 多 key / URL 标签回显先脱敏后裁剪、白名单、敏感 fixture 正文不落盘、异常不打印 payload |
| 14. 真实 Agent 通知 | 实际 Codex notify receipt；实际 Claude Stop hook_response，均仅连接模拟上游 |
| 15. 停止 / 恢复 / 重启原路径 | test_guard_lifecycle.py 真子进程 start/status/stop/restart，CLI detach 原字节恢复后原上游可调用 |

## 开销测量

同一 Python 进程内的 loopback 客户端、上游与网关；Chat JSON 与 Chat SSE 固定合成实体，并发 1，无人工延迟。每模式 10 warmup + 100 measured。三模式按 direct → observation_disabled → observation_enabled 顺序运行；观测队列容量 4096（**产品默认 256**），单事件 65536 bytes，检测 timeout 2 秒，未丢观察。

| 负载 / 模式 | 完整响应中位 ms | p95 ms | 整批 CPU 秒 | Python heap 峰值 bytes |
| --- | ---: | ---: | ---: | ---: |
| JSON 直连 | 2.036 | 5.577 | 0.170 | 318944 |
| JSON 仅代理 | 6.174 | 31.720 | 0.427 | 332365 |
| JSON 开启观测 | 10.749 | 44.365 | 0.740 | 648869 |
| SSE 直连 | 2.154 | 6.759 | 0.208 | 314951 |
| SSE 仅代理 | 3.970 | 8.420 | 0.364 | 327198 |
| SSE 开启观测 | 6.873 | 9.950 | 0.724 | 596211 |

CPU 包含整进程客户端、上游、网关及后台队列排空；内存是 tracemalloc 的 Python 分配峰值，**不是 RSS**。所有模式启用 tracemalloc，它本身带来开销。结果是此主机此负载的描述性测量，不是跨设备性能保证。JSON/SSE response bytes、写块数、首字节/完整响应分布与逐请求样本都保存在 benchmark.json。无延迟的 SSE 多次写入可被 HTTP 合并；增量可见性由 held 上游协议测试单独证明。没有“零延迟”承诺。

## CI 与未完成项

仓库配置 Python 3.10 / 3.12 / 3.13 的 Actions：固定依赖、113 项测试、模拟告警链路、每组 30 次基准与 legacy demo/plan。CI 不要求安装 Codex/Claude，也不调用真实模型。实际 CI 状态必须以该 commit 的 [Actions](https://github.com/zhouey314-cloud/relay-audit/actions) 为准，本地通过不代替 GitHub 运行结果；最终交付记录保存实际 run URL 与 head commit。

未完成：真实 API / 中转流量启用、真实模型身份校准、误报漏报率、原生 WebSocket/Gemini、多模态、MCP 服务、OS 进程监督安装器和系统弹窗目视确认。没有声称已保护正在执行任务的 Codex 连接。
