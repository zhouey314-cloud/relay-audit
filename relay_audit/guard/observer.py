"""Bounded, passive protocol observation; never returns response content.

The forwarding layer owns the wire bytes. This observer only receives a copy
and may lose observability without changing, delaying, or reconstructing them.
"""

import json
import time
from collections import deque


PROTOCOLS = {
    "responses": "responses", "/v1/responses": "responses",
    "chat_completions": "chat_completions", "chat": "chat_completions",
    "/v1/chat/completions": "chat_completions",
    "anthropic_messages": "anthropic_messages", "messages": "anthropic_messages",
    "/v1/messages": "anthropic_messages",
}
USAGE_NUMBERS = {
    "input_tokens", "output_tokens", "total_tokens", "prompt_tokens", "completion_tokens",
    "cache_creation_input_tokens", "cache_read_input_tokens", "cached_tokens",
}
USAGE_DETAILS = {
    "input_tokens_details": {"cached_tokens", "cache_write_tokens", "audio_tokens", "image_tokens", "text_tokens"},
    "prompt_tokens_details": {"cached_tokens", "cache_write_tokens", "audio_tokens", "image_tokens", "text_tokens"},
    "output_tokens_details": {"reasoning_tokens", "audio_tokens", "text_tokens"},
    "completion_tokens_details": {"reasoning_tokens", "audio_tokens", "accepted_prediction_tokens", "rejected_prediction_tokens"},
    "cache_creation": {"ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens"},
}
STOP_REASONS = {
    "stop": "completed", "end_turn": "completed", "stop_sequence": "completed",
    "tool_calls": "tool_use", "function_call": "tool_use", "tool_use": "tool_use",
    "length": "output_budget", "max_tokens": "output_budget", "max_output_tokens": "output_budget",
    "content_filter": "incomplete", "refusal": "incomplete", "pause_turn": "incomplete",
    "model_context_window_exceeded": "incomplete", "steered": "incomplete",
}
MAX_METADATA_EVENTS = 128
MAX_MODEL_LABELS = 128
MAX_TOOL_INDICES = 128


def _json(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    def invalid(_):
        raise ValueError("nonstandard JSON number")

    return json.loads(data, object_pairs_hook=unique, parse_constant=invalid)


def _usage(value):
    """Provider-reported counters only; no arbitrary fields or string values."""
    result = {}
    if not isinstance(value, dict):
        return result
    for key in USAGE_NUMBERS:
        count = value.get(key)
        if type(count) is int and count >= 0:
            result[key] = count
    for key, allowed in USAGE_DETAILS.items():
        detail = value.get(key)
        if isinstance(detail, dict):
            filtered = {name: number for name, number in detail.items()
                        if name in allowed and type(number) is int and number >= 0}
            if filtered:
                result[key] = filtered
    return result


class Observer:
    def __init__(self, protocol, request_meta=None, max_event_bytes=65536):
        if type(max_event_bytes) is not int or max_event_bytes < 64:
            raise ValueError("max_event_bytes must be an integer >= 64")
        self.protocol = PROTOCOLS.get(protocol, "unsupported")
        self.max_event_bytes = max_event_bytes
        # Keep no request body. The proxy supplies timing/request identifiers.
        self._started = time.monotonic()
        self._first_visible = None
        self._line = bytearray()
        self._line_size = 0
        self._event_bytes = 0
        self._data = []
        self._event_name = ""
        self._skip_lf = False
        self._dropping = False
        self._events = deque()
        self._models = []
        self._last_model = None
        self._usage = {}
        self._degraded = []
        self._protocol_errors = []
        self._parse_errors = 0
        self._oversized = 0
        self._event_count = 0
        self._unknown_count = 0
        self._tool_indices = set()
        self._tool_seen = False
        self._tool_fragments = 0
        self._termination = None
        self._finish_reason = None
        self._terminal = False
        self._closed = False
        if self.protocol == "unsupported":
            self._degrade("unsupported_protocol")

    @property
    def buffered_bytes(self):
        """Bytes of content retained while waiting for a complete SSE event."""
        return len(self._line) + sum(len(item) for item in self._data)

    @property
    def first_visible_output_ms(self):
        """Observation timestamp only; no response content is exposed."""
        return self._first_visible

    def _emit(self, event):
        if len(self._events) >= MAX_METADATA_EVENTS:
            if "event_queue_overflow" not in self._degraded:
                self._degraded.append("event_queue_overflow")
            # Always retain the degradation signal; other metadata is bounded.
            self._events.popleft()
            self._events.append({"type": "monitoring_degraded", "reason": "event_queue_overflow"})
            return
        self._events.append(event)

    def _degrade(self, reason):
        if reason not in self._degraded:
            self._degraded.append(reason)
            self._emit({"type": "monitoring_degraded", "reason": reason})

    def _protocol_error(self, reason):
        if reason not in self._protocol_errors:
            self._protocol_errors.append(reason)
            self._emit({"type": "protocol_error", "reason": reason})

    def drain_events(self):
        events = list(self._events)
        self._events.clear()
        return events

    def _model(self, value):
        if value is None:
            return
        if not isinstance(value, str) or not value or len(value) > 200:
            self._degrade("model_label_unobservable")
            return
        if value != self._last_model:
            self._last_model = value
            if len(self._models) < MAX_MODEL_LABELS:
                self._models.append(value)
            else:
                self._degrade("model_label_history_overflow")
            self._emit({"type": "model_label", "model": value})

    def _meta(self, value):
        if not isinstance(value, dict):
            return
        self._model(value.get("model"))
        counters = _usage(value.get("usage"))
        for key, item in counters.items():
            if isinstance(item, dict):
                self._usage.setdefault(key, {}).update(item)
            else:
                self._usage[key] = item

    def _visible(self, text):
        if isinstance(text, str) and text and self._first_visible is None:
            self._first_visible = round((time.monotonic() - self._started) * 1000, 3)

    def _stop(self, reason):
        if not isinstance(reason, str):
            return
        # Do not retain an arbitrary provider string masquerading as a reason.
        self._finish_reason = reason if reason in STOP_REASONS else "unknown"
        self._termination = STOP_REASONS.get(reason, "incomplete")

    def _tool(self, index=None):
        self._tool_seen = True
        if type(index) is int and index >= 0:
            if len(self._tool_indices) < MAX_TOOL_INDICES:
                self._tool_indices.add(index)
            elif index not in self._tool_indices:
                self._degrade("tool_metadata_overflow")

    def feed(self, chunk):
        if self._closed:
            return
        if not isinstance(chunk, bytes):
            self._degrade("invalid_chunk_type")
            return
        offset = 0
        while offset < len(chunk):
            if self._skip_lf:
                self._skip_lf = False
                if chunk[offset] == 10:
                    offset += 1
                    continue
            cr = chunk.find(b"\r", offset)
            lf = chunk.find(b"\n", offset)
            ends = [position for position in (cr, lf) if position >= 0]
            end = min(ends) if ends else len(chunk)
            size = end - offset
            self._line_size += size
            self._event_bytes += size
            if not self._dropping and self._event_bytes > self.max_event_bytes:
                self._dropping = True
                self._oversized += 1
                self._line.clear()
                self._data.clear()
                self._event_name = ""
                self._degrade("event_too_large")
            if not self._dropping:
                self._line.extend(chunk[offset:end])
            offset = end
            if offset == len(chunk):
                break
            self._skip_lf = chunk[offset] == 13
            self._end_line()
            offset += 1

    def _end_line(self):
        blank = self._line_size == 0
        if blank:
            if not self._dropping and self._data:
                self._event(b"\n".join(self._data), self._event_name)
            self._event_bytes = 0
            self._dropping = False
            self._data.clear()
            self._event_name = ""
        elif not self._dropping:
            line = bytes(self._line)
            # Ignore comments and all unrelated SSE fields without decoding them.
            name, separator, value = line.partition(b":")
            if separator and value.startswith(b" "):
                value = value[1:]
            if name == b"data":
                self._data.append(value)
            elif name == b"event":
                try:
                    self._event_name = value.decode("utf-8")
                except UnicodeDecodeError:
                    self._parse_errors += 1
                    self._degrade("invalid_utf8")
        self._line.clear()
        self._line_size = 0

    def _event(self, raw, event_name):
        self._event_count += 1
        if self.protocol == "chat_completions" and raw.strip() == b"[DONE]":
            self._terminal = True
            if self._termination is None:
                self._termination = "incomplete"
                self._protocol_error("missing_finish_reason")
            return
        try:
            data = _json(raw)
            if not isinstance(data, dict):
                raise ValueError("event data must be an object")
            self._observe(data, event_name, streaming=True)
        except (ValueError, TypeError, KeyError, IndexError, AttributeError, RecursionError, UnicodeDecodeError):
            self._parse_errors += 1
            self._degrade("event_parse_error")

    def _observe(self, data, event_name="", streaming=True):
        self._meta(data)
        for key in ("response", "message", "delta", "content_block", "item"):
            self._meta(data.get(key))
        if isinstance(data.get("error"), dict):
            self._terminal = True
            self._termination = "failed"
            return
        if self.protocol == "responses":
            self._responses(data, event_name, streaming)
        elif self.protocol == "chat_completions":
            self._chat(data, streaming)
        elif self.protocol == "anthropic_messages":
            self._anthropic(data, event_name, streaming)

    def _responses(self, data, event_name, streaming):
        kind = data.get("type", event_name)
        if not streaming:
            self._response_output(data.get("output", []))
            self._response_terminal(data.get("status"), data)
            return
        if event_name and data.get("type") and event_name != data["type"]:
            self._protocol_error("event_type_mismatch")
        if kind in {"response.output_text.delta", "response.refusal.delta", "response.output_text.done", "response.refusal.done"}:
            field = "delta" if kind.endswith(".delta") else ("text" if "output_text" in kind else "refusal")
            text = data[field]
            if not isinstance(text, str):
                raise ValueError("text delta must be a string")
            self._visible(text)
        elif kind in {"response.output_item.added", "response.output_item.done"}:
            self._response_output([data["item"]])
        elif kind == "response.function_call_arguments.delta":
            if not isinstance(data.get("delta"), str):
                self._protocol_error("invalid_tool_call")
            self._tool(data.get("output_index"))
            self._tool_fragments += 1
        elif kind in {"response.completed", "response.incomplete", "response.failed"}:
            response = data.get("response")
            if not isinstance(response, dict):
                raise ValueError("terminal response missing")
            self._response_output(response.get("output", []))
            self._response_terminal(kind.split(".")[1], response)
        elif kind == "error":
            self._terminal = True
            self._termination = "failed"
        elif kind not in {"response.created", "response.in_progress", "response.function_call_arguments.done",
                          "response.output_text.done", "response.content_part.added", "response.content_part.done"}:
            self._unknown_count += 1

    def _response_terminal(self, status, response):
        if status == "completed":
            self._terminal = True
            self._termination = "tool_use" if self._tool_seen else "completed"
        elif status == "incomplete":
            self._terminal = True
            detail = response.get("incomplete_details")
            reason = detail.get("reason") if isinstance(detail, dict) else None
            self._stop(reason)
            self._termination = self._termination or "incomplete"
        elif status in {"failed", "cancelled"}:
            self._terminal = True
            self._termination = "failed" if status == "failed" else "upstream_cancelled"
        else:
            self._degrade("response_status_unobservable")

    def _response_output(self, output):
        if not isinstance(output, list):
            raise ValueError("output must be a list")
        for item in output:
            if not isinstance(item, dict):
                raise ValueError("output item must be an object")
            if item.get("type") == "function_call":
                self._tool()
                if not isinstance(item.get("name"), str) or not isinstance(item.get("arguments", ""), str):
                    self._protocol_error("invalid_tool_call")
            elif item.get("type") == "message":
                content = item.get("content", [])
                if not isinstance(content, list):
                    raise ValueError("message content must be a list")
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "output_text":
                        self._visible(part.get("text"))

    def _chat(self, data, streaming):
        if "error" in data:
            self._terminal = True
            self._termination = "failed"
            return
        choices = data.get("choices")
        if not isinstance(choices, list):
            raise ValueError("choices must be a list")
        for choice in choices:
            if not isinstance(choice, dict):
                raise ValueError("choice must be an object")
            message = choice.get("delta" if streaming else "message", {})
            if not isinstance(message, dict):
                raise ValueError("message must be an object")
            self._visible(message.get("content"))
            calls = message.get("tool_calls", [])
            if not isinstance(calls, list):
                self._protocol_error("invalid_tool_call")
            else:
                for call in calls:
                    self._tool(call.get("index") if isinstance(call, dict) else None)
                    if not isinstance(call, dict):
                        self._protocol_error("invalid_tool_call")
                        continue
                    function = call.get("function", {})
                    if not isinstance(function, dict) or any(
                            key in function and not isinstance(function[key], str) for key in ("name", "arguments")):
                        self._protocol_error("invalid_tool_call")
                    if isinstance(function, dict) and "arguments" in function:
                        self._tool_fragments += 1
            self._stop(choice.get("finish_reason"))
        if not streaming:
            self._terminal = bool(choices)
            if self._termination is None:
                self._termination = "incomplete"
                self._protocol_error("missing_finish_reason")

    def _anthropic(self, data, event_name, streaming):
        kind = data.get("type", event_name)
        if event_name and data.get("type") and event_name != data["type"]:
            self._protocol_error("event_type_mismatch")
        if not streaming:
            content = data.get("content")
            if not isinstance(content, list):
                raise ValueError("content must be a list")
            for block in content:
                self._anthropic_block(block)
            self._stop(data.get("stop_reason"))
            self._terminal = self._termination is not None
            if not self._terminal:
                self._degrade("stop_reason_unobservable")
        elif kind == "content_block_start":
            self._anthropic_block(data.get("content_block"), data.get("index"))
        elif kind == "content_block_delta":
            delta = data.get("delta")
            if not isinstance(delta, dict):
                raise ValueError("delta must be an object")
            if delta.get("type") == "text_delta":
                if not isinstance(delta.get("text"), str):
                    raise ValueError("text delta must be a string")
                self._visible(delta["text"])
            elif delta.get("type") == "input_json_delta":
                self._tool(data.get("index"))
                self._tool_fragments += 1
                if not isinstance(delta.get("partial_json"), str):
                    self._protocol_error("invalid_tool_call")
        elif kind == "message_delta":
            delta = data.get("delta")
            if not isinstance(delta, dict):
                raise ValueError("message delta must be an object")
            self._stop(delta.get("stop_reason"))
        elif kind == "message_stop":
            self._terminal = True
            if self._termination is None:
                self._termination = "incomplete"
                self._protocol_error("missing_stop_reason")
        elif kind == "error":
            self._terminal = True
            self._termination = "failed"
        elif kind not in {"message_start", "content_block_stop", "ping"}:
            self._unknown_count += 1

    def _anthropic_block(self, block, index=None):
        if not isinstance(block, dict):
            raise ValueError("content block must be an object")
        if block.get("type") == "text":
            self._visible(block.get("text"))
        elif block.get("type") == "tool_use":
            self._tool(index)
            if not isinstance(block.get("name"), str) or not isinstance(block.get("input"), dict):
                self._protocol_error("invalid_tool_call")

    def finish(self, cancelled=False):
        if not self._closed:
            # SSE dispatch requires a blank line. A partial final event is not
            # invented into a successful terminal response.
            if self.buffered_bytes or self._line_size:
                self._data.clear()
                self._line.clear()
            self._event_name = ""
            self._closed = True
        if cancelled:
            termination = "client_cancelled"
        elif self._terminal:
            termination = self._termination or "incomplete"
        elif self._degraded:
            termination = "unobservable"
        else:
            termination = "upstream_truncated"
        return {
            "protocol": self.protocol, "model_labels": list(self._models),
            "model_label_observable": bool(self._models), "identity_status": "unverified",
            "usage_provider_reported": dict(self._usage), "usage_source": "upstream_reported",
            "first_visible_output_ms": self._first_visible, "event_count": self._event_count,
            "unknown_event_count": self._unknown_count, "stream_complete": self._terminal,
            "termination": termination, "finish_reason": self._finish_reason,
            "cancelled": bool(cancelled), "monitoring_degraded": list(self._degraded),
            "parse_errors": self._parse_errors, "oversized_events": self._oversized,
            "protocol_errors": list(self._protocol_errors), "tool_use_observed": self._tool_seen,
            "tool_argument_fragments": self._tool_fragments,
            "tool_arguments_validation": "not_performed_fragmented" if self._tool_fragments else "not_performed",
        }


def observe_json(protocol, bodybytes, request_meta=None, max_event_bytes=65536):
    """Observe a bounded non-streaming body without retaining its content."""
    observer = Observer(protocol, request_meta, max_event_bytes)
    if not isinstance(bodybytes, bytes):
        observer._degrade("invalid_body_type")
    elif len(bodybytes) > max_event_bytes:
        observer._oversized += 1
        observer._degrade("body_too_large")
    else:
        try:
            data = _json(bodybytes)
            if not isinstance(data, dict):
                raise ValueError("body must be an object")
            observer._observe(data, streaming=False)
        except (ValueError, TypeError, KeyError, IndexError, AttributeError, RecursionError, UnicodeDecodeError):
            observer._parse_errors += 1
            observer._degrade("body_parse_error")
    return observer.finish()
