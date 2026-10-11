import json
import unittest
from unittest.mock import patch

from relay_audit.guard.observer import Observer, observe_json, MAX_METADATA_EVENTS


PRIVATE = "SYNTHETIC-PRIVATE-CODE-ANSWER-REASONING-ARGUMENTS"


def sse(data, event=None, ending="\n"):
    name = "event: " + event + ending if event else ""
    return (name + "data: " + json.dumps(data, ensure_ascii=False) + ending + ending).encode()


class ObserverTests(unittest.TestCase):
    def responses(self):
        return [
            sse({"type": "response.created", "response": {"model": "model-A"}}),
            sse({"type": "response.output_text.delta", "delta": "你好🙂"}),
            sse({"type": "response.completed", "response": {"model": "model-A", "status": "completed", "usage": {"input_tokens": 10, "output_tokens": 2}}}),
        ]

    def test_all_protocols_network_and_utf8_split_positions(self):
        wires = {
            "responses": b"".join(self.responses()),
            "chat_completions": sse({"model": "model-A", "choices": [{"delta": {"role": "assistant"}}]})
                + sse({"model": "model-A", "choices": [{"delta": {"content": "你好🙂"}}]})
                + sse({"choices": [{"delta": {}, "finish_reason": "stop"}]}) + b"data: [DONE]\n\n",
            "anthropic_messages": sse({"type": "message_start", "message": {"model": "model-A"}})
                + sse({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "你好🙂"}})
                + sse({"type": "message_delta", "delta": {"stop_reason": "end_turn"}}) + sse({"type": "message_stop"}),
        }
        for protocol, wire in wires.items():
            for position in range(len(wire) + 1):
                with self.subTest(protocol=protocol, position=position):
                    observer = Observer(protocol)
                    observer.feed(wire[:position])
                    observer.feed(wire[position:])
                    result = observer.finish()
                    self.assertTrue(result["stream_complete"])
                    self.assertEqual(result["termination"], "completed")
                    self.assertEqual(result["model_labels"], ["model-A"])
                    self.assertIsNotNone(result["first_visible_output_ms"])
                    self.assertEqual(result["monitoring_degraded"], [])

    def test_multiline_data_crlf_split_and_comment_heartbeats(self):
        wire = (b": heartbeat\r\n\r\nevent: response.created\r\n"
                b'data: {"type":"response.created",\r\n'
                b'data: "response":{"model":"model-A"}}\r\n\r\n'
                b'event: response.completed\r\ndata: {"type":"response.completed","response":{"status":"completed"}}\r\n\r\n')
        observer = Observer("responses")
        for byte in wire:
            observer.feed(bytes([byte]))
        result = observer.finish()
        self.assertEqual(result["event_count"], 2)
        self.assertEqual(result["model_labels"], ["model-A"])
        self.assertIsNone(result["first_visible_output_ms"])
        self.assertEqual(result["termination"], "completed")

    def test_model_changes_are_available_before_finish_in_all_protocols(self):
        cases = {
            "responses": [sse({"type": "response.created", "response": {"model": "A"}}),
                          sse({"type": "response.in_progress", "response": {"model": "B"}}),
                          sse({"type": "response.in_progress", "response": {"model": "A"}})],
            "chat_completions": [sse({"model": model, "choices": [{"delta": {"role": "assistant"}}]}) for model in ("A", "B", "A")],
            "anthropic_messages": [sse({"type": "message_start", "message": {"model": "A"}}),
                                   sse({"type": "message_delta", "delta": {"model": "B"}}),
                                   sse({"type": "message_delta", "delta": {"model": "A"}})],
        }
        for protocol, chunks in cases.items():
            observer = Observer(protocol)
            changes = []
            for chunk in chunks:
                observer.feed(chunk)
                changes.extend(event["model"] for event in observer.drain_events() if event["type"] == "model_label")
            self.assertEqual(changes, ["A", "B", "A"])
            self.assertEqual(observer.finish()["model_labels"], ["A", "B", "A"])

    def test_first_visible_output_excludes_role_ping_reasoning_and_tool_fragments(self):
        with patch("relay_audit.guard.observer.time.monotonic", side_effect=[1.0, 1.5]):
            observer = Observer("anthropic_messages")
            observer.feed(sse({"type": "message_start", "message": {"role": "assistant", "model": "A"}}))
            observer.feed(sse({"type": "ping"}))
            observer.feed(sse({"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": PRIVATE}}))
            observer.feed(sse({"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": PRIVATE}}))
            self.assertIsNone(observer.finish()["first_visible_output_ms"])
            # New observer is used because finish closes the first one.
        with patch("relay_audit.guard.observer.time.monotonic", side_effect=[1.0, 1.5]):
            observer = Observer("anthropic_messages")
            observer.feed(sse({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Visible"}}))
            self.assertEqual(observer.finish()["first_visible_output_ms"], 500.0)

    def test_each_protocol_keeps_its_terminal_form_and_output_budget(self):
        cases = [
            ("responses", sse({"type": "response.incomplete", "response": {"incomplete_details": {"reason": "max_output_tokens"}}}), "output_budget"),
            ("responses", sse({"type": "response.failed", "response": {"error": {"message": PRIVATE}}}), "failed"),
            ("responses", sse({"type": "response.incomplete", "response": {"incomplete_details": {"reason": "content_filter"}}}), "incomplete"),
            ("chat_completions", sse({"choices": [{"delta": {}, "finish_reason": "length"}]}) + b"data: [DONE]\n\n", "output_budget"),
            ("anthropic_messages", sse({"type": "message_delta", "delta": {"stop_reason": "max_tokens"}}) + sse({"type": "message_stop"}), "output_budget"),
            ("anthropic_messages", sse({"type": "message_delta", "delta": {"stop_reason": "tool_use"}}) + sse({"type": "message_stop"}), "tool_use"),
        ]
        for protocol, wire, termination in cases:
            with self.subTest(protocol=protocol, termination=termination):
                observer = Observer(protocol)
                observer.feed(wire)
                result = observer.finish()
                self.assertTrue(result["stream_complete"])
                self.assertEqual(result["termination"], termination)
                self.assertNotIn(PRIVATE, json.dumps(result))

    def test_chat_finish_reason_without_done_is_still_truncated(self):
        observer = Observer("chat_completions")
        observer.feed(sse({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}))
        result = observer.finish()
        self.assertFalse(result["stream_complete"])
        self.assertEqual(result["termination"], "upstream_truncated")
        self.assertEqual(result["finish_reason"], "tool_calls")

    def test_cancel_missing_terminal_and_parse_loss_are_separate(self):
        healthy = Observer("responses")
        healthy.feed(self.responses()[0])
        self.assertEqual(healthy.finish()["termination"], "upstream_truncated")
        cancelled = Observer("responses")
        cancelled.feed(self.responses()[0])
        self.assertEqual(cancelled.finish(cancelled=True)["termination"], "client_cancelled")
        broken = Observer("responses")
        broken.feed(b"data: {not json}\n\n")
        result = broken.finish()
        self.assertEqual(result["termination"], "unobservable")
        self.assertEqual(result["parse_errors"], 1)
        self.assertIn("event_parse_error", result["monitoring_degraded"])

    def test_oversized_event_is_bounded_and_recovers_without_upstream_accusation(self):
        observer = Observer("responses", max_event_bytes=256)
        for _ in range(100):
            observer.feed(b"x" * 100)
            self.assertLessEqual(observer.buffered_bytes, 256)
        result = observer.finish()
        self.assertEqual(result["termination"], "unobservable")
        self.assertEqual(result["oversized_events"], 1)
        observer = Observer("responses", max_event_bytes=256)
        observer.feed(b"data: " + b"x" * 10000 + b"\n\n")
        observer.feed(self.responses()[-1])
        result = observer.finish()
        self.assertTrue(result["stream_complete"])
        self.assertIn("event_too_large", result["monitoring_degraded"])

    def test_queue_overflow_is_visible_and_bounded(self):
        observer = Observer("responses")
        for number in range(MAX_METADATA_EVENTS + 10):
            observer.feed(sse({"type": "response.created", "response": {"model": "model-" + str(number)}}))
        events = observer.drain_events()
        self.assertLessEqual(len(events), MAX_METADATA_EVENTS)
        self.assertTrue(any(event["type"] == "monitoring_degraded" for event in events))
        self.assertIn("event_queue_overflow", observer.finish()["monitoring_degraded"])

    def test_unknown_events_and_sensitive_fields_are_not_saved(self):
        observer = Observer("responses", {"prompt": PRIVATE, "answer": PRIVATE, "Authorization": PRIVATE})
        observer.feed(sse({"type": "response.future_extension", "secret": PRIVATE, "reasoning": PRIVATE}))
        observer.feed(sse({"type": "response.output_text.delta", "delta": PRIVATE}))
        observer.feed(sse({"type": "response.function_call_arguments.delta", "output_index": 0, "delta": '{"secret":"' + PRIVATE + '"'}))
        observer.feed(self.responses()[-1])
        result = observer.finish()
        self.assertEqual(result["unknown_event_count"], 1)
        self.assertEqual(result["tool_argument_fragments"], 1)
        self.assertNotIn(PRIVATE, json.dumps(result) + json.dumps(observer.drain_events()) + repr(observer.__dict__))

    def test_tool_fragments_are_not_parsed_as_complete_arguments(self):
        cases = {
            "responses": sse({"type": "response.function_call_arguments.delta", "output_index": 0, "delta": '{"a":'}) + self.responses()[-1],
            "chat_completions": sse({"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"a":'}}]}}]}) + sse({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}) + b"data: [DONE]\n\n",
            "anthropic_messages": sse({"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "name": "f", "input": {}}}) + sse({"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"a":'}}) + sse({"type": "message_delta", "delta": {"stop_reason": "tool_use"}}) + sse({"type": "message_stop"}),
        }
        for protocol, wire in cases.items():
            observer = Observer(protocol)
            observer.feed(wire)
            result = observer.finish()
            self.assertTrue(result["stream_complete"])
            self.assertEqual(result["termination"], "tool_use")
            self.assertEqual(result["protocol_errors"], [])
            self.assertEqual(result["monitoring_degraded"], [])

    def test_invalid_tool_shape_is_reported_without_arguments(self):
        observer = Observer("chat_completions")
        observer.feed(sse({"choices": [{"delta": {"tool_calls": [{"function": {"arguments": {"private": PRIVATE}}}]}, "finish_reason": "tool_calls"}]}))
        observer.feed(b"data: [DONE]\n\n")
        result = observer.finish()
        self.assertEqual(result["protocol_errors"], ["invalid_tool_call"])
        self.assertNotIn(PRIVATE, json.dumps(result))

    def test_usage_cache_whitelist_and_cumulative_messages_usage(self):
        observer = Observer("anthropic_messages")
        observer.feed(sse({"type": "message_start", "message": {"model": "A", "usage": {"input_tokens": 10, "output_tokens": 1, "cache_read_input_tokens": 8, "cache_creation": {"ephemeral_5m_input_tokens": 2, "secret": PRIVATE}, "secret": PRIVATE}}}))
        observer.feed(sse({"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 5}}))
        observer.feed(sse({"type": "message_stop"}))
        result = observer.finish()
        self.assertEqual(result["usage_provider_reported"], {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 8, "cache_creation": {"ephemeral_5m_input_tokens": 2}})
        self.assertEqual(result["usage_source"], "upstream_reported")
        self.assertNotIn(PRIVATE, json.dumps(result))

    def test_invalid_utf8_duplicate_json_and_deep_json_are_observation_loss(self):
        for wire in (b'data: {"delta":"\xff"}\n\n',
                     b'data: {"type":"response.created","type":"response.completed"}\n\n',
                     b'data: ' + b'[' * 2000 + b'0' + b']' * 2000 + b'\n\n'):
            observer = Observer("responses")
            observer.feed(wire)
            result = observer.finish()
            self.assertEqual(result["termination"], "unobservable")
            self.assertGreater(result["parse_errors"], 0)

    def test_wrong_protocol_terminal_is_not_a_success(self):
        observer = Observer("responses")
        observer.feed(b"data: [DONE]\n\n")
        self.assertFalse(observer.finish()["stream_complete"])
        observer = Observer("anthropic_messages")
        observer.feed(sse({"type": "response.completed", "response": {"status": "completed"}}))
        self.assertEqual(observer.finish()["termination"], "upstream_truncated")


class JsonObservationTests(unittest.TestCase):
    def test_all_nonstream_protocols_and_output_budget(self):
        cases = [
            ("responses", {"model": "A", "status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": PRIVATE}]}]}, "completed"),
            ("responses", {"model": "A", "status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}}, "output_budget"),
            ("chat_completions", {"model": "A", "choices": [{"message": {"content": PRIVATE}, "finish_reason": "stop"}], "usage": {"prompt_tokens_details": {"cached_tokens": 5, "secret": PRIVATE}}}, "completed"),
            ("anthropic_messages", {"model": "A", "content": [{"type": "tool_use", "name": "f", "input": {"secret": PRIVATE}}], "stop_reason": "tool_use"}, "tool_use"),
        ]
        for protocol, body, termination in cases:
            result = observe_json(protocol, json.dumps(body).encode())
            self.assertTrue(result["stream_complete"])
            self.assertEqual(result["termination"], termination)
            self.assertEqual(result["model_labels"], ["A"])
            self.assertNotIn(PRIVATE, json.dumps(result))

    def test_nonstream_errors_limits_and_missing_model_are_explicit(self):
        failed = observe_json("anthropic_messages", json.dumps({"type": "error", "error": {"message": PRIVATE}}).encode())
        self.assertEqual(failed["termination"], "failed")
        self.assertFalse(failed["model_label_observable"])
        self.assertNotIn(PRIVATE, json.dumps(failed))
        missing = observe_json("responses", b'{"status":"completed","output":[]}')
        self.assertFalse(missing["model_label_observable"])
        self.assertEqual(missing["identity_status"], "unverified")
        big = observe_json("responses", b"x" * 1000, max_event_bytes=256)
        self.assertEqual(big["termination"], "unobservable")
        self.assertEqual(big["monitoring_degraded"], ["body_too_large"])
        invalid = observe_json("responses", b"not-json")
        self.assertEqual(invalid["termination"], "unobservable")


if __name__ == "__main__":
    unittest.main()
