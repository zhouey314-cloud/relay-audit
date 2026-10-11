# Agent 接入、通知与恢复

Agent 的 HTTP 模型请求必须经过 Guard 的监听地址。MCP 或在提示词中要求 Agent 检查报告都不会自动截获模型流量。真实模型身份始终为 `unverified`；本机 Mock 验收不代表识别过真实中转。

## Codex：独立 CODEX_HOME 与 Responses SSE

本次目标客户端为 `codex-cli 0.162.0-alpha.17.2`。官方当前配置支持自定义 provider 的 `base_url`、`env_key`、`wire_api = "responses"`、`requires_openai_auth = false` 和 `supports_websockets = false`。本版将独立测试配置固定为 HTTP/SSE，WebSocket 未实现。

Codex 0.134.0 起，profile 是 `$CODEX_HOME/<name>.config.toml`，使用 `-p <name>` 选择。旧 `[profiles.<name>]` 结构不适用于本次客户端。provider 与 `notify` 属于用户层配置，不能放到项目 `.codex/config.toml` 并期待生效。

使用 `guard attach-codex` 生成独立目录中的 `relayguard.config.toml` 与 `relayguard-launch.py`；CLI 的实际参数见 `python3 -m relay_audit guard attach-codex --help`。配置仅保存令牌环境变量名称，命令返回需要设置的环境与启动参数。启动生成器返回的 `launch_command`，例如：

```bash
python3 /absolute/path/to/isolated-codex-home/relayguard-launch.py \
  --skip-git-repo-check "your task"
```

先在该子进程环境设置 `RELAY_GUARD_TOKEN`，与网关的本地令牌相同。上游 API Key 留在网关进程环境中。这里的本地令牌不是官方订阅 OAuth 凭据。适配器拒绝修改 `~/.codex` 及其子目录；不会读取或复制 `auth.json`、系统钥匙串或当前连接。

本机 alpha 实测：`--ignore-user-config -p relayguard` 的组合未采用 profile provider。生成的 launcher 因而同时使用明确的 CLI provider、URL、凭证环境变量、SSE 与 notify 覆盖；不能只复制这两个 flags 后认定接入已生效。第一轮错误配置曾向默认官方端点发出无认证请求，全部被 401 拒绝，没有真实模型请求成功。修复后的验证由本机网关计数与客户端完成输出确认。

当前 provider 示例：

```toml
model = "user-selected-model"
model_provider = "relayguard"

[model_providers.relayguard]
name = "RelayAudit AgentGuard"
base_url = "http://127.0.0.1:8765/v1"
env_key = "RELAY_GUARD_TOKEN"
wire_api = "responses"
requires_openai_auth = false
supports_websockets = false
request_max_retries = 0
stream_max_retries = 0
```

适配器还设置 `notify` 为 Python 通知程序。官方支持的该入口当前事件是 `agent-turn-complete`：每个完成回合运行一次外部程序。它不是任意请求完成回调，也不是聊天框推送 API。网关独立终端在规则触发时立即提醒；notify 在完成回合读取新事件、输出固定提醒到其 stderr，并写入 `hook_receipts.jsonl` 证明生命周期程序执行。客户端可能捕获外部程序 stderr，因此不能仅凭 receipt 宣称聊天框显示了告警。

Codex 的通知 JSON 包含用户消息和最后回答。适配器完全忽略该参数，不保存正文、不读取 transcript、不将告警塞回模型响应。可选 macOS 系统通知是独立渠道；系统接收提交不证明用户肉眼看到弹窗。

## Claude Code：显式 API 路线与 Stop hook

生成器 `attach_claude(...)` 创建独立 `relayguard-settings.json`，不修改 `~/.claude`，包含 `ANTHROPIC_BASE_URL` 与 `Stop` command hook。用 `--settings` 指定该文件，用 `--setting-sources ""` 排除用户/项目配置，用 `--no-session-persistence` 限制本地会话持久化。必须同时给子进程一个明确的本地 `ANTHROPIC_API_KEY` 或 `ANTHROPIC_AUTH_TOKEN`，不能只改变 URL：只改 URL 可能仍使用已保存的订阅凭据。

本地验收只使用假的本地令牌与模拟上游。`--bare` 会跳过 hooks，不能用于验证 Stop hook。实际客户端测试应移除继承的 OAuth、云 provider 与真实 API 环境变量，使用独立 `CLAUDE_CONFIG_DIR`，关闭非必要流量与所有模型工具，并确保所有指定模型端点为 loopback。

Stop 在主 Agent 完成回答时触发，不涵盖用户中断。API 错误的 StopFailure 是另一事件，本版不声称已接入。通知程序忽略 stdin 的 hook JSON，正常退出、不返回 block 或 additionalContext，因此不会令 Agent 继续额外请求。Claude 的 hook stderr 是否显示取决于客户端；本版保留独立网关终端提醒，不宣传通用聊天框推送。

## 恢复

适配器保存安装文件的 SHA-256 以及已有文件的备份和 SHA-256。`guard detach` 接收返回的 manifest 路径；只有安装内容和备份均未变化才恢复。接入期间用户改了配置时，恢复拒绝覆盖并保留后续修改。新建配置则删除该文件；已有配置恢复原字节内容。停止网关后需退出使用该 profile 的 Agent，选择原配置或原 CODEX_HOME 再启动；网关崩溃不会自动变成直连。

配置文件与 manifest 中的路径绑定当前安装位置。移动项目后应重新生成适配器。配置生成、实际客户端请求、hook 调用、终端提醒、系统弹窗和真实流量监测是不同验收项，不能相互替代。

## 官方依据（2026-10-11 查阅）

- [OpenAI Advanced Configuration：profile、provider 与 notify](https://learn.chatgpt.com/docs/config-file/config-advanced)
- [OpenAI Configuration Reference：provider 与配置层限制](https://learn.chatgpt.com/docs/config-file/config-reference)
- [Claude Code LLM Gateway：URL 和认证凭据](https://code.claude.com/docs/en/llm-gateway)
- [Claude Code Hooks：Stop lifecycle 和输入正文](https://code.claude.com/docs/en/hooks#stop)


Claude CLI 示例（只用于已经明确配置的 API 路线）：

```bash
relay-audit guard attach-claude --config guard.local.json \
  --settings-dir ./claude-guard-settings --model your-model
# 在专用子进程环境提供本地令牌，移除订阅 OAuth 的环境选择器。
env -u CLAUDE_CODE_OAUTH_TOKEN -u ANTHROPIC_AUTH_TOKEN \
  CLAUDE_CONFIG_DIR="$PWD/claude-guard-settings/client-home" \
  ANTHROPIC_API_KEY="$RELAY_GUARD_TOKEN" \
  claude --settings ./claude-guard-settings/relayguard-settings.json \
  --setting-sources "" --no-session-persistence -p "你的任务"
```

上例的 gateway token 只送到 loopback，不是上游 Key。Claude 的通知已在真实本机客户端的 `hook_response` 中验收；它是独立生命周期输出。Codex 只验收外部 notify 执行与网关终端告警，未验收聊天框内提示。两个客户端的真实 API 路线都尚未启用。
