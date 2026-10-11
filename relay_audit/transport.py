import hashlib
from http.client import HTTPException
import json
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from .probes import canonical

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
TOOL = {
    "name": "record_result", "description": "Record a synthetic audit result",
    "parameters": {"type": "object", "properties": {
        "item": {"type": "string"}, "count": {"type": "integer"}},
        "required": ["item", "count"], "additionalProperties": False},
}


@dataclass
class Result:
    answer: str | None = None
    error: str | None = None
    grading_error: str | None = None
    status_code: int | None = None
    latency_ms: float = 0
    reported_model: str | None = None
    finish_reason: str | None = None
    usage: dict = field(default_factory=dict)


def payload(ep, probe):
    request = {"model": ep["model"], "messages": [{"role": "user", "content": probe.prompt}],
               **ep["parameters"]}
    if ep["protocol"] == "anthropic_messages":
        request["max_tokens"] = ep["output_limit"]
        if probe.tool:
            request["tools"] = [{"name": TOOL["name"], "description": TOOL["description"],
                                 "input_schema": TOOL["parameters"]}]
            request["tool_choice"] = {"type": "tool", "name": TOOL["name"]}
    else:
        request[ep["limit_field"]] = ep["output_limit"]
        if probe.tool:
            request["tools"] = [{"type": "function", "function": TOOL}]
            request["tool_choice"] = {"type": "function", "function": {"name": TOOL["name"]}}
    return request


def extract(ep, data, probe):
    if not isinstance(data, dict):
        raise ValueError("Response must be an object")
    grading_error = None
    if ep["protocol"] == "anthropic_messages":
        blocks = data["content"]
        if not isinstance(blocks, list) or any(not isinstance(block, dict) for block in blocks):
            raise ValueError("content must be a list")
        texts = [block["text"] for block in blocks if block.get("type") == "text"]
        if any(not isinstance(text, str) for text in texts):
            raise ValueError("text must be a string")
        if probe.tool:
            calls = [block for block in blocks if block.get("type") == "tool_use"]
            if any(not isinstance(call.get("name"), str) or not isinstance(call.get("input"), dict)
                   for call in calls):
                raise ValueError("Malformed tool_use block")
            answer = canonical(calls[0]["input"]) if len(calls) == 1 else None
            if len(calls) != 1:
                grading_error = "tool_call_count"
            elif calls[0]["name"] != TOOL["name"]:
                grading_error = "tool_selection"
            elif any(texts):
                grading_error = "unexpected_text"
        else:
            answer = "".join(texts)
        finish = data.get("stop_reason")
    else:
        choice = data["choices"][0]
        msg = choice["message"]
        if not isinstance(msg, dict):
            raise ValueError("message must be an object")
        if probe.tool:
            calls = msg.get("tool_calls", [])
            if not isinstance(calls, list):
                raise ValueError("tool_calls must be a list")
            for call in calls:
                if not isinstance(call, dict) or not isinstance(call.get("function"), dict):
                    raise ValueError("Malformed tool call")
                function = call["function"]
                if not isinstance(function.get("name"), str) or not isinstance(function.get("arguments"), str):
                    raise ValueError("Malformed tool function")
            text = msg.get("content")
            if text is not None and not isinstance(text, str):
                raise ValueError("content must be a string or null")
            answer = calls[0]["function"]["arguments"] if len(calls) == 1 else None
            if len(calls) != 1:
                grading_error = "tool_call_count"
            elif calls[0]["function"]["name"] != TOOL["name"]:
                grading_error = "tool_selection"
            elif text:
                grading_error = "unexpected_text"
        else:
            answer = msg.get("content")
        finish = choice.get("finish_reason")
    if not isinstance(answer, str) and grading_error is None:
        raise ValueError("Expected string answer")
    source_usage = data.get("usage")
    usage = {} if not isinstance(source_usage, dict) else {
        key: value for key, value in source_usage.items()
        if key in {"prompt_tokens", "completion_tokens", "total_tokens", "input_tokens", "output_tokens"}
        and type(value) is int and value >= 0
    }
    reported = data.get("model")
    # Redact credentials in the runner before truncating provider-controlled metadata.
    return Result(answer=answer, grading_error=grading_error,
                  reported_model=reported if isinstance(reported, str) else None,
                  finish_reason=finish if isinstance(finish, str) else None, usage=usage)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HTTPClient:
    def __init__(self):
        # Ignore implicit proxy env vars: send the key only to the configured endpoint.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def call(self, ep, probe, key, timeout):
        headers = {"Content-Type": "application/json", "Accept": "application/json",
                   "User-Agent": "relay-audit-lite/0.1.0"}
        if ep["protocol"] == "anthropic_messages":
            headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
        else:
            headers["Authorization"] = "Bearer " + key
        request = urllib.request.Request(ep["url"], data=canonical(payload(ep, probe)).encode(),
                                         headers=headers, method="POST")
        start = time.monotonic()
        observed_status = None
        try:
            with self.opener.open(request, timeout=timeout) as response:
                observed_status = response.status
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    result = Result(error="response_too_large", status_code=response.status)
                else:
                    result = extract(ep, json.loads(raw), probe)
                    result.status_code = response.status
        except urllib.error.HTTPError as exc:
            # Never retain the response error body, headers, URL, or exception repr.
            result = Result(error="redirect_blocked" if 300 <= exc.code < 400 else "http_error",
                            status_code=exc.code)
            exc.close()
        except (TimeoutError, socket.timeout):
            result = Result(error="timeout")
        except (urllib.error.URLError, OSError):
            result = Result(error="network_error")
        except (HTTPException, KeyError, IndexError, ValueError, TypeError, AttributeError, RecursionError):
            result = Result(error="invalid_response")
        if result.status_code is None:
            result.status_code = observed_status
        result.latency_ms = round((time.monotonic() - start) * 1000, 3)
        return result


class MockClient:
    """A controlled demonstration; this is NOT a real model benchmark."""

    def call(self, ep, probe, key, timeout):
        answer = probe.expected
        mode = ep.get("mock_mode", "clean")
        draw = int(hashlib.sha256(probe.id.encode()).hexdigest()[:8], 16) % 5
        if mode == "degraded" and (probe.family.startswith("context_") or draw == 0):
            answer = {"simulated_incorrect": True}
        if mode == "degraded" and probe.family == "tool_arguments" and draw == 1:
            return Result(error="http_error", status_code=400, latency_ms=80)
        return Result(answer=canonical(answer), status_code=200,
                      latency_ms=220 if mode == "clean" else 90,
                      reported_model=ep["model"], finish_reason="stop")
