"""Local synthetic upstream and Agent harness. Never calls an external model."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shutil
import socket
import sys
import time
from typing import Any

from aiohttp import ClientSession, web


def _sse(data: dict | str, event: str | None = None) -> bytes:
    encoded = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return ((f"event: {event}\r\n" if event else "") + f"data: {encoded}\r\n\r\n").encode()


def _response(model: str, number: int, text: str, terminal: str = "completed", tool: bool = False) -> dict:
    output = ([{"id": f"fc_{number}", "type": "function_call", "status": "completed",
                "call_id": f"call_{number}", "name": "mock_lookup", "arguments": '{"city":"杭州"}'}]
              if tool else [{"id": f"msg_{number}", "type": "message", "role": "assistant",
                             "status": "completed", "content": [{"type": "output_text", "text": text, "annotations": []}]}])
    return {"id": f"resp_mock_{number}", "object": "response", "created_at": int(time.time()),
            "model": model, "status": terminal, "output": output,
            "error": {"code": "server_error", "message": "synthetic failure"} if terminal == "failed" else None,
            "incomplete_details": {"reason": "max_output_tokens"} if terminal == "incomplete" else None,
            "usage": {"input_tokens": 10, "input_tokens_details": {"cached_tokens": 3},
                      "output_tokens": 4, "output_tokens_details": {"reasoning_tokens": 0}, "total_tokens": 14},
            "parallel_tool_calls": True, "tools": [], "tool_choice": "auto", "metadata": {}}


def _responses_events(response: dict) -> list[bytes]:
    number = response["id"].rsplit("_", 1)[-1]
    initial = {**response, "status": "in_progress", "output": [], "usage": None}
    events = [_sse({"type": "response.created", "response": initial}, "response.created"),
              _sse({"type": "response.in_progress", "response": initial}, "response.in_progress")]
    item = response["output"][0]
    is_tool = item["type"] == "function_call"
    added = {**item, "status": "in_progress", "arguments": ""} if is_tool else {**item, "status": "in_progress", "content": []}
    events.append(_sse({"type": "response.output_item.added", "output_index": 0, "item": added}, "response.output_item.added"))
    if is_tool:
        for delta in ['{"city":', '"杭', '州"}']:
            events.append(_sse({"type": "response.function_call_arguments.delta", "item_id": item["id"],
                                "output_index": 0, "delta": delta}, "response.function_call_arguments.delta"))
        events.append(_sse({"type": "response.function_call_arguments.done", "item_id": item["id"], "output_index": 0,
                            "arguments": item["arguments"]}, "response.function_call_arguments.done"))
    else:
        part = item["content"][0]
        events.append(_sse({"type": "response.content_part.added", "item_id": item["id"], "output_index": 0,
                            "content_index": 0, "part": {**part, "text": ""}}, "response.content_part.added"))
        text = part["text"]
        for delta in [text[:5], text[5:]]:
            events.append(_sse({"type": "response.output_text.delta", "item_id": item["id"], "output_index": 0,
                                "content_index": 0, "delta": delta, "logprobs": []}, "response.output_text.delta"))
        events.append(_sse({"type": "response.output_text.done", "item_id": item["id"], "output_index": 0,
                            "content_index": 0, "text": text, "logprobs": []}, "response.output_text.done"))
        events.append(_sse({"type": "response.content_part.done", "item_id": item["id"], "output_index": 0,
                            "content_index": 0, "part": part}, "response.content_part.done"))
    events.append(_sse({"type": "response.output_item.done", "output_index": 0, "item": item}, "response.output_item.done"))
    terminal = "response." + response["status"]
    events.append(_sse({"type": terminal, "response": response}, terminal))
    return events


def _chat_response(model: str, number: int, text: str, tool: bool, terminal: str) -> dict:
    message = {"role": "assistant", "content": None if tool else text}
    if tool:
        message["tool_calls"] = [{"id": f"call_{number}", "type": "function", "function":
                                  {"name": "mock_lookup", "arguments": '{"city":"杭州"}'}}]
    return {"id": f"chatcmpl_mock_{number}", "object": "chat.completion", "created": int(time.time()), "model": model,
            "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if tool else ("length" if terminal == "incomplete" else "stop")}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14,
                      "prompt_tokens_details": {"cached_tokens": 3}}}


def _chat_events(response: dict) -> list[bytes]:
    chunk = {k: response[k] for k in ["id", "created", "model"]}
    chunk["object"] = "chat.completion.chunk"
    message = response["choices"][0]["message"]
    events = [_sse({**chunk, "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]})]
    if message.get("tool_calls"):
        call = message["tool_calls"][0]
        for i, fragment in enumerate(['{"city":', '"杭', '州"}']):
            function = {"arguments": fragment}
            if i == 0:
                function["name"] = "mock_lookup"
            delta = {"tool_calls": [{"index": 0, **({"id": call["id"], "type": "function"} if i == 0 else {}), "function": function}]}
            events.append(_sse({**chunk, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}))
    else:
        text = message["content"]
        for delta in [text[:5], text[5:]]:
            events.append(_sse({**chunk, "choices": [{"index": 0, "delta": {"content": delta}, "finish_reason": None}]}))
    events += [_sse({**chunk, "choices": [{"index": 0, "delta": {}, "finish_reason": response["choices"][0]["finish_reason"]}]}),
               _sse({**chunk, "choices": [], "usage": response["usage"]}), _sse("[DONE]")]
    return events


def _messages_response(model: str, number: int, text: str, tool: bool, terminal: str) -> dict:
    content = ([{"type": "tool_use", "id": f"toolu_{number}", "name": "mock_lookup", "input": {"city": "杭州"}}]
               if tool else [{"type": "text", "text": text}])
    return {"id": f"msg_mock_{number}", "type": "message", "role": "assistant", "model": model, "content": content,
            "stop_reason": "tool_use" if tool else ("max_tokens" if terminal == "incomplete" else "end_turn"),
            "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 4,
                                               "cache_read_input_tokens": 3, "cache_creation_input_tokens": 0}}


def _messages_events(response: dict) -> list[bytes]:
    initial = {**response, "content": [], "stop_reason": None, "usage": {**response["usage"], "output_tokens": 0}}
    block = response["content"][0]
    tool = block["type"] == "tool_use"
    events = [_sse({"type": "message_start", "message": initial}, "message_start"),
              _sse({"type": "ping"}, "ping"),
              _sse({"type": "content_block_start", "index": 0, "content_block": {**block, "input": {}} if tool else {"type": "text", "text": ""}}, "content_block_start")]
    pieces = ['{"city":', '"杭', '州"}'] if tool else [block["text"][:5], block["text"][5:]]
    for piece in pieces:
        delta = {"type": "input_json_delta", "partial_json": piece} if tool else {"type": "text_delta", "text": piece}
        events.append(_sse({"type": "content_block_delta", "index": 0, "delta": delta}, "content_block_delta"))
    events += [_sse({"type": "content_block_stop", "index": 0}, "content_block_stop"),
               _sse({"type": "message_delta", "delta": {"stop_reason": response["stop_reason"], "stop_sequence": None},
                     "usage": {"output_tokens": 4}}, "message_delta"),
               _sse({"type": "message_stop"}, "message_stop")]
    return events


def create_mock_app(scenario: dict[str, Any] | None = None) -> web.Application:
    """Create test-only upstream with programmable labels, errors and chunks.

    scenario: labels, text, chunk_sizes, chunk_delay, http_statuses,
    terminal (completed/incomplete/failed), disconnect_after, tool_first.
    State stores counts and request metadata, never authentication or input text.
    """
    scenario = dict(scenario or {})
    app = web.Application()
    state = {"synthetic": True, "request_count": 0, "requests": [], "disconnects": 0}
    app["mock_state"] = state

    async def handle(request: web.Request) -> web.StreamResponse:
        if request.path.endswith("/models"):
            return web.json_response({"object": "list", "data": [{"id": "mock-model", "object": "model", "owned_by": "synthetic"}]})
        raw = await request.read()
        body = json.loads(raw) if raw else {}
        if request.path.endswith("/count_tokens"):
            return web.json_response({"input_tokens": 10})
        state["request_count"] += 1
        number = state["request_count"]
        model = body.get("model", "mock-model")
        labels = scenario.get("labels") or [model]
        label = labels[min(number - 1, len(labels) - 1)]
        inputs = body.get("input") if isinstance(body.get("input"), list) else body.get("messages", [])
        tool_output_present = any(i.get("type") in {"function_call_output", "tool_result"} or i.get("role") == "tool"
                                  or any(block.get("type") == "tool_result" for block in (i.get("content") or []) if isinstance(block, dict))
                                  for i in inputs if isinstance(i, dict))
        state["requests"].append({"number": number, "path": request.path, "model": model,
                                  "stream": bool(body.get("stream")), "body_bytes": len(raw),
                                  "tool_output_present": tool_output_present})
        statuses = scenario.get("http_statuses", [200])
        status = statuses[min(number - 1, len(statuses) - 1)]
        if status != 200:
            return web.json_response({"error": {"type": "synthetic_error", "message": "controlled mock error"}}, status=status)
        text = scenario.get("text", "MOCK_AGENT_OK 模拟回答 ✓")
        tool = bool(scenario.get("tool_first") and number == 1)
        terminal = scenario.get("terminal", "completed")
        if request.path.endswith("/responses"):
            result = _response(label, number, text, terminal, tool)
            events = _responses_events(result)
        elif request.path.endswith("/chat/completions"):
            result = _chat_response(label, number, text, tool, terminal)
            events = _chat_events(result)
        else:
            result = _messages_response(label, number, text, tool, terminal)
            events = _messages_events(result)
        if not body.get("stream"):
            return web.json_response(result)
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"})
        await response.prepare(request)
        data = b"".join(events)
        sizes = scenario.get("chunk_sizes", [1, 2, 7, 19, 53])
        cursor = 0
        index = 0
        try:
            while cursor < len(data):
                size = max(1, int(sizes[index % len(sizes)]))
                await response.write(data[cursor:cursor + size])
                cursor += size
                index += 1
                if scenario.get("disconnect_after") and cursor >= scenario["disconnect_after"]:
                    request.transport.close()
                    return response
                if scenario.get("chunk_delay", 0):
                    await asyncio.sleep(scenario["chunk_delay"])
            await response.write_eof()
        except (ConnectionResetError, asyncio.CancelledError):
            state["disconnects"] += 1
        return response

    for route in ["/v1/responses", "/v1/chat/completions", "/v1/messages", "/v1/messages/count_tokens", "/v1/models"]:
        app.router.add_route("GET" if route.endswith("/models") else "POST", route, handle)
    return app


async def run_harness(base_url: str, token: str, *, protocol: str = "responses",
                      model: str = "mock-model", count: int = 3, tool_multi_turn: bool = False,
                      session_id: str = "synthetic-harness-session") -> dict:
    """Send synthetic Agent requests, optionally following a synthetic tool call."""
    routes = {"responses": "/responses", "chat_completions": "/chat/completions", "anthropic_messages": "/messages"}
    if protocol not in routes:
        raise ValueError("unsupported harness protocol")
    observations = []
    async with ClientSession() as session:
        for index in range(count):
            body: dict = {"model": model, "stream": True}
            if protocol == "responses":
                body["input"] = [{"role": "user", "content": "synthetic Agent task"}]
                if tool_multi_turn and index == 1:
                    body["input"] += [{"type": "function_call", "call_id": "call_1", "name": "mock_lookup", "arguments": '{"city":"杭州"}'},
                                      {"type": "function_call_output", "call_id": "call_1", "output": "synthetic tool result"}]
            else:
                body["messages"] = [{"role": "user", "content": "synthetic Agent task"}]
                if protocol == "anthropic_messages":
                    body["max_tokens"] = 64
                    if tool_multi_turn and index == 1:
                        body["messages"] += [{"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_1", "name": "mock_lookup", "input": {"city": "杭州"}}]},
                                             {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "synthetic tool result"}]}]
                elif tool_multi_turn and index == 1:
                    body["messages"] += [{"role": "assistant", "content": None, "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "mock_lookup", "arguments": '{"city":"杭州"}'}}]},
                                         {"role": "tool", "tool_call_id": "call_1", "content": "synthetic tool result"}]
            if tool_multi_turn:
                schema = {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}
                if protocol == "anthropic_messages":
                    body["tools"] = [{"name": "mock_lookup", "description": "synthetic lookup", "input_schema": schema}]
                elif protocol == "responses":
                    body["tools"] = [{"type": "function", "name": "mock_lookup", "description": "synthetic lookup", "parameters": schema}]
                else:
                    body["tools"] = [{"type": "function", "function": {"name": "mock_lookup", "description": "synthetic lookup", "parameters": schema}}]
            started = time.perf_counter()
            async with session.post(base_url.rstrip("/") + routes[protocol], json=body,
                                    headers={"Authorization": "Bearer " + token, "x-api-key": token,
                                             "X-Session-Id": session_id}) as response:
                data = await response.read()
                observations.append({"number": index + 1, "http_status": response.status, "response_bytes": len(data),
                                     "seconds": round(time.perf_counter() - started, 6)})
    return {"synthetic": True, "identity_status": "unverified", "protocol": protocol,
            "agent_requests": len(observations), "observations": observations}


async def _serve(app: web.Application):
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    sock.setblocking(False)
    port = sock.getsockname()[1]
    await web.SockSite(runner, sock).start()
    return runner, f"http://127.0.0.1:{port}"


async def run_demo(output_dir: str | Path) -> dict:
    """Normal → changed label → automatic alert → continued synthetic requests."""
    from .adapters import notify
    from .proxy import create_app
    root = Path(output_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=False)
    mock = create_mock_app({"labels": ["mock-model", "mock-other", "mock-other"]})
    upstream_runner, upstream_url = await _serve(mock)
    names = ("RELAY_GUARD_DEMO_LOCAL_TOKEN", "RELAY_GUARD_DEMO_UPSTREAM_KEY")
    previous = {key: os.environ.get(key) for key in names}
    for key in names:
        os.environ[key] = "synthetic-local-token-do-not-use-for-real-api"
    guard_runner = None
    try:
        config = {"upstream": {"base_url": upstream_url + "/v1", "key_env": names[1],
                               "alias": "synthetic", "allow_local_http": True},
                  "local_token_env": names[0], "state_dir": str(root / "state"), "mode": "passive"}
        guard = create_app(config)
        guard_runner, guard_url = await _serve(guard)
        result = await run_harness(guard_url + "/v1", os.environ[names[0]])
        gateway = guard["gateway"]
        await asyncio.wait_for(gateway.flush(), 5)
        await asyncio.wait_for(gateway.notifier.queue.join(), 5)
        receipt = notify(root / "state", "harness")
        incidents = gateway.store.read(100)
        report = gateway.store.report(root / "report.md")
        summary = {**result, "evidence_status": "synthetic_unverified", "identity_status": "unverified",
                   "upstream_requests": mock["mock_state"]["request_count"],
                   "extra_model_requests": 0, "continued_after_alert": result["observations"][-1]["http_status"] == 200,
                   "incident_types": sorted({i["event_type"] for i in incidents}),
                   "notification": gateway.notifier.snapshot(), "hook_receipt": receipt,
                   "report": report, "status": gateway.status(), "real_api_verified": False}
        (root / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
        return summary
    finally:
        if guard_runner is not None:
            await guard_runner.cleanup()
        await upstream_runner.cleanup()
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


async def check_local_agent(output_dir: str | Path, agent: str = "codex") -> dict:
    """Execute an installed client against a fake-key, loopback-only test path.

    This proves client protocol/config/hook mechanics, not real model identity.
    Run explicitly; this function is not part of the default unit-test suite.
    """
    from .adapters import attach_codex, attach_claude, detach
    from .proxy import create_app
    if agent not in {"codex", "claude"}:
        raise ValueError("unsupported actual Agent check")
    executable = shutil.which(agent)
    if not executable:
        return {"agent": agent, "status": "NOT_RUN", "reason": "client_not_installed", "real_api_verified": False}
    root = Path(output_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=False)
    client_cwd = root / "client-work"
    client_cwd.mkdir()
    model = "gpt-6.1-sol" if agent == "codex" else "claude-sonnet-5"
    mock = create_mock_app({"labels": ["mock-changed-label"], "text": "MOCK_AGENT_ACCEPTANCE_OK 模拟",
                            "chunk_sizes": [3, 19, 59, 113], "chunk_delay": .001})
    upstream_runner, upstream_url = await _serve(mock)
    names = ("RELAY_GUARD_ACCEPTANCE_TOKEN", "RELAY_GUARD_ACCEPTANCE_UPSTREAM_KEY")
    old = {name: os.environ.get(name) for name in names}
    os.environ[names[0]] = "fake-local-agent-token"
    os.environ[names[1]] = "fake-upstream-key"
    guard_runner = None
    result = {}
    try:
        config = {"upstream": {"base_url": upstream_url + "/v1", "key_env": names[1],
                               "alias": "synthetic-actual-client", "allow_local_http": True},
                  "local_token_env": names[0], "state_dir": str(root / "state"), "mode": "passive"}
        guard = create_app(config)
        guard_runner, guard_url = await _serve(guard)
        # Inherit no real API/OAuth credentials, proxy routing or provider selectors.
        environment = {key: os.environ[key] for key in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR") if key in os.environ}
        environment["NO_PROXY"] = "127.0.0.1,localhost"
        if agent == "codex":
            attached = attach_codex(codex_home=root / "codex-home", guard_url=guard_url,
                                    model=model, state_dir=root / "state", local_token_env=names[0])
            environment["CODEX_HOME"] = attached["codex_home"]
            environment[names[0]] = os.environ[names[0]]
            argv = [*attached["launch_command"], "--strict-config", "--skip-git-repo-check",
                    "--color", "never", "--json", "Reply with one brief synthetic test sentence. Do not call tools."]
        else:
            attached = attach_claude(settings_dir=root / "claude-settings", guard_url=guard_url,
                                     model=model, state_dir=root / "state")
            environment.update({"CLAUDE_CONFIG_DIR": str(root / "claude-home"),
                                "ANTHROPIC_API_KEY": os.environ[names[0]], "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"})
            argv = [executable, "--settings", attached["configuration_files"][0], "--setting-sources", "",
                    "--no-session-persistence", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                    "--no-chrome", "--disable-slash-commands", "--tools", "", "--model", model,
                    "--output-format", "stream-json", "--verbose", "--include-hook-events", "-p",
                    "Reply with one brief synthetic test sentence. Do not call tools."]
        process = await asyncio.create_subprocess_exec(*argv, cwd=client_cwd, env=environment,
                                                       stdin=asyncio.subprocess.DEVNULL,
                                                       stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        timed_out = False
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), 60)
        except asyncio.TimeoutError:
            timed_out = True
            process.kill()
            stdout, stderr = await process.communicate()
        gateway = guard["gateway"]
        await asyncio.wait_for(gateway.flush(), 5)
        await asyncio.wait_for(gateway.notifier.queue.join(), 5)
        # notify processes are external and can finish just after client exit.
        await asyncio.sleep(.3)
        receipts_path = root / "state" / "hook_receipts.jsonl"
        receipts = [json.loads(line) for line in receipts_path.read_text().splitlines()] if receipts_path.exists() else []
        (root / "client.stdout.jsonl").write_bytes(stdout)
        (root / "client.stderr.txt").write_bytes(stderr)
        incidents = gateway.store.read(100)
        restore = detach(attached["manifest_path"])
        version = await asyncio.create_subprocess_exec(executable, "--version", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=environment)
        version_out, _ = await version.communicate()
        result = {"synthetic_upstream": True, "real_client": True, "agent": agent, "client_version": version_out.decode().strip(),
                  "exit_code": process.returncode, "timed_out": timed_out,
                  "response_marker_received": b"MOCK_AGENT_ACCEPTANCE_OK" in stdout,
                  "hook_called": bool(receipts), "hook_new_incidents": sum(r.get("new_incidents", 0) for r in receipts),
                  "hook_stderr_visible_in_client_capture": b"RelayAudit AgentGuard" in stderr or b"RelayAudit AgentGuard" in stdout,
                  "upstream_requests": mock["mock_state"]["request_count"],
                  "upstream_request_metadata": mock["mock_state"]["requests"],
                  "incident_types": sorted({i["event_type"] for i in incidents}),
                  "notifications": gateway.notifier.snapshot(), "detached": restore["detached"],
                  "identity_status": "unverified", "real_api_verified": False,
                  "status": "PASS" if process.returncode == 0 and b"MOCK_AGENT_ACCEPTANCE_OK" in stdout and receipts else "INCOMPLETE"}
        (root / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        gateway.store.report(root / "report.md")
        return result
    finally:
        if guard_runner:
            await guard_runner.cleanup()
        await upstream_runner.cleanup()
        for name, value in old.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
