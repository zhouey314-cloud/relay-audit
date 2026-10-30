# AgentGuard 运行容量与失败排查

这个工具面向已有 Agent/API 路线的开发者：保留业务 HTTP/SSE 数据，独立记录可见协议事件、异常与排查线索。接入不需要复制历史聊天或修改订阅配置。

## 明确的请求容量

`max_inflight_requests` 默认 16，可在配置中设为 1–256 的整数。一个名额覆盖请求体读取、上游请求和完整流；结束、超时、异常和取消都会释放。超出容量的请求在本地返回 503 和 `Retry-After: 1`，不会发给上游，也不由网关自动重试。正在进行的流继续完成。

这与已有的 parser/detector 队列分开：业务请求过载是明确的 HTTP 503；观察队列丢失仍显示 monitoring_degraded，保留正常转发。

使用既有 `guard status` 查看：

- `inflight_requests`：当前占用；
- `max_inflight_requests`：配置上限；
- `capacity_rejected`：本进程启动以来被容量拒绝的请求数；
- 既有 forwarded/observed/dropped 和 monitoring 状态。

认证与状态仍要求本地凭据；拒绝重复 Authorization，或仅使用 x-api-key 时的重复字段。兼容既有 Authorization 优先行为，其余客户端凭据继续从上游转发中删除。

## 验证

新增回归实际打开一个挂起的 SSE，占满名额后确认第二请求 503、上游数量不增加，释放后可再次请求；也验证 429 后名额回收、重复凭据不触达上游。原 125 条回归完整保留，当前 128 条通过。并发 4 / 每档 30 请求的同一 loopback 合成验收通过。

测试说明机制在这些负载下工作，不建立生产容量承诺。模型声明始终 identity_status=unverified；容量限制不参与模型身份推断或检测阈值。
