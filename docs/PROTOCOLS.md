# Guard 协议支持与模拟 Harness

HTTP/SSE 代理支持固定上游的 `POST /v1/responses`、`POST /v1/chat/completions`、`POST /v1/messages`，以及模型列表和 Messages token count 辅助路由。辅助路由也只能对应一个客户端请求，不自动轮询上游。原生 WebSocket、Gemini、多模态与任意应用流量拦截不在当前验收范围。

Responses、Chat Completions、Anthropic Messages 使用各自结束语义：`response.completed` / `response.incomplete` / `response.failed`；choice `finish_reason` 与 `[DONE]`；`message_delta.stop_reason` 与 `message_stop`。工具调用不是普通文本，合法工具结束与输出预算结束不应当作换模证据。未知 SSE 事件仍须保留转发。

`relay_audit.guard.harness.create_mock_app(scenario)` 创建仅供本地测试的 aiohttp 上游。它不调用模型、不持有真实 Key，不保存请求正文。`app["mock_state"]` 记录合成请求计数、路径、请求模型、流模式、请求规模与工具输出存在标记。计数用于验证 passive 中 N 个请求对应 N 次上游调用。

可控制场景如下：

| 参数 | 行为 |
| --- | --- |
| `labels` | 按请求序号返回标签；最后一个标签持续使用 |
| `text` | 合成输出，默认含 Unicode |
| `chunk_sizes` | HTTP 写入块大小循环；默认跨 UTF-8、JSON 与 SSE 边界切分 |
| `chunk_delay` | 每块延迟，用于取消与延迟场景 |
| `http_statuses` | 按序号返回受控 HTTP 错误 |
| `terminal` | Responses 的 completed / incomplete / failed；其他协议采用对应合法停止原因 |
| `disconnect_after` | 到指定响应字节数时关闭模拟连接 |
| `tool_first` | 第一次响应为工具调用，参数按片段发送；后续响应为文本 |

`run_harness(base_url, token, protocol="responses", model="mock-model", count=3, tool_multi_turn=False)` 向本机端点连续发送独立合成任务，返回 HTTP 状态、响应字节数与耗时。可选合成工具结果用于多轮协议验证，不执行模型生成的工具代码。模拟工具输出、usage 和缓存信息只用于验证实体保留，不能证明服务商实际计费或模型能力。

实际 Codex 验收必须通过当前客户端的 `/v1/responses` HTTP/SSE 链路完成；同协议模拟 Harness 成功不能代替客户端验收。Claude Code 同理。真实模型身份校准和真实中转流量尚未验收。
