"""Seeded synthetic tasks with local, deterministic answer keys."""

import hashlib
import json
import random
from dataclasses import dataclass

PROBE_VERSION = "synthetic-v1"
FAMILIES = ("modular", "ledger", "json_schema", "unicode", "code_trace",
            "context_1024", "context_4096", "context_16384", "tool_arguments")


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class Probe:
    id: str
    family: str
    category: str
    prompt: str
    expected: dict
    tool: bool = False

    @property
    def digest(self):
        return hashlib.sha256(canonical({"version": PROBE_VERSION, "prompt": self.prompt,
                                         "tool": self.tool}).encode()).hexdigest()


def build(seed, variants=3, families=None):
    selected = set(FAMILIES if families is None else families)
    if selected - set(FAMILIES):
        raise ValueError("Unknown family: " + ", ".join(sorted(selected - set(FAMILIES))))
    tasks = []
    for variant in range(variants):
        rng = random.Random((seed << 16) + variant)
        a, b, modulus = rng.randint(21, 95), rng.randint(11, 89), rng.choice([97, 101, 113])
        prompts = []
        prompts.append(("modular", "reasoning",
                        f"Compute ({a}^3 + {b}^2) mod {modulus}. Return ONLY JSON {{\"answer\": integer}}.",
                        {"answer": (a**3 + b**2) % modulus}, False))
        amounts = [rng.randint(-50, 90) for _ in range(12)]
        prompts.append(("ledger", "reasoning",
                        f"Amounts: {canonical(amounts)}. Sum only strictly positive amounts at even ZERO-based indices. "
                        "Return ONLY JSON {\"answer\": integer}.",
                        {"answer": sum(v for i, v in enumerate(amounts) if i % 2 == 0 and v > 0)}, False))
        values = [rng.randint(-9, 9) for _ in range(9)]
        prompts.append(("json_schema", "structured_output",
                        f"Input {canonical(values)}. Return ONLY an object with EXACT keys \"positive\" "
                        "(positive values sorted descending, duplicates preserved) and \"count\" "
                        "(total input count). No markdown or extra keys.",
                        {"positive": sorted([v for v in values if v > 0], reverse=True), "count": len(values)}, False))
        token = f"审计Ω-🙂-{rng.getrandbits(32):08x}-e\u0301"
        prompts.append(("unicode", "instruction_following",
                        f"Copy this string without normalizing Unicode: {canonical(token)}. "
                        "Return ONLY JSON {\"value\": the_exact_string}.", {"value": token}, False))
        nums = [rng.randint(-7, 12) for _ in range(11)]
        expected = []
        for number in nums:
            if expected and number < expected[-1]:
                expected.pop()
            else:
                expected.append(number)
        code = ("def solve(nums):\n    out = []\n    for n in nums:\n"
                "        if out and n < out[-1]:\n            out.pop()\n"
                "        else:\n            out.append(n)\n    return out")
        prompts.append(("code_trace", "code_tracing",
                        f"Trace this Python code precisely:\n{code}\nInput: {canonical(nums)}. "
                        "Return ONLY JSON {\"answer\": solve(input)}.", {"answer": expected}, False))
        for length in (1024, 4096, 16384):
            anchors = {key: f"v-{rng.getrandbits(64):016x}" for key in ("alpha", "beta", "gamma")}
            noise = " ".join(f"record_{i:05d}={rng.getrandbits(32):08x}" for i in range(length // 22 + 1))
            noise = noise[:length]
            midpoint = noise.rfind(" ", 0, len(noise) // 2)
            # Anchors stay separate from noise records, including the middle anchor.
            context = (f"alpha={anchors['alpha']}\n{noise[:midpoint]}\n"
                       f"beta={anchors['beta']}\n{noise[midpoint:]}\ngamma={anchors['gamma']}")
            prompts.append((f"context_{length}", "context_retrieval",
                            "Read this synthetic record collection and retrieve the exact values of alpha, beta, gamma. "
                            "Return ONLY JSON with those three keys.\nBEGIN RECORDS\n" + context + "\nEND RECORDS",
                            anchors, False))
        item = f"sample-{rng.getrandbits(32):08x}"
        prompts.append(("tool_arguments", "tool_calling",
                        f"Call the supplied record_result function exactly once with item={canonical(item)} "
                        f"and count={len(values)}. Do not respond in ordinary text.",
                        {"item": item, "count": len(values)}, True))
        for family, category, prompt, answer, tool in prompts:
            if family in selected:
                tasks.append(Probe(f"{family}:{variant}", family, category, prompt, answer, tool))
    return tasks


def strict_json(text):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def invalid(value):
        raise ValueError("nonstandard numeric literal")

    return json.loads(text, object_pairs_hook=unique, parse_constant=invalid)


def grade(probe, answer):
    try:
        actual = strict_json(answer)
        # Canonical JSON distinguishes booleans, floats, and integer answer keys.
        passed = canonical(actual) == canonical(probe.expected)
    except (ValueError, TypeError, RecursionError):
        return False, "invalid_json"
    return passed, "matched" if passed else "answer_mismatch"
