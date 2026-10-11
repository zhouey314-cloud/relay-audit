import html
import json
from collections import Counter, defaultdict
from statistics import median

from .config import conditions

LIMITATION = ("Capability evidence only. Model identity and operator intent are NOT verified. "
              "Small synthetic suites can saturate and can be recognized by a relay.")


def summarize(config, records, status, synthetic=False):
    endpoints = {}
    for ep in config["endpoints"]:
        rows = [row for row in records if row["endpoint"] == ep["name"]]
        graded = [row for row in rows if row["passed"] is not None]
        categories = defaultdict(list)
        for row in graded:
            categories[row["category"]].append(row["passed"])
        endpoints[ep["name"]] = {
            "attempts": len(rows), "graded": len(graded),
            "passes": sum(row["passed"] for row in graded),
            "errors": dict(Counter(row["error"] for row in rows if row["error"])),
            "median_latency_ms": median(row["latency_ms"] for row in rows) if rows else None,
            "reported_models_unverified": sorted({row["reported_model"] for row in rows if row["reported_model"]}),
            "categories": {category: {"passes": sum(values), "graded": len(values)}
                           for category, values in sorted(categories.items())},
        }
    comparisons = []
    reference = next((ep for ep in config["endpoints"] if ep["role"] == "reference"), None)
    if reference:
        ref_rows = {row["probe_digest"]: row for row in records
                    if row["endpoint"] == reference["name"]}
        for ep in config["endpoints"]:
            if ep["name"] == reference["name"]:
                continue
            pairs = []
            for row in records:
                other = ref_rows.get(row["probe_digest"])
                if row["endpoint"] == ep["name"] and other is not None:
                    if row["passed"] is not None and other["passed"] is not None:
                        pairs.append((row, other))
            compatible = conditions(ep) == conditions(reference)
            delta = (sum(row["passed"] - other["passed"] for row, other in pairs) / len(pairs)
                     if pairs else None)
            comparisons.append({
                "target": ep["name"], "reference": reference["name"], "paired_graded": len(pairs),
                "declared_conditions_match": compatible,
                "snapshot_equivalence": "user_declared_not_independently_verified",
                "pass_rate_delta_pp": round(delta * 100, 2) if delta is not None else None,
                "interpretation": "descriptive_only" if compatible and pairs else "inconclusive_conditions_or_samples",
                "identity_verdict": "not_verified",
            })
    return {"schema_version": 1, "synthetic": synthetic, "run_status": status,
            "limitation": LIMITATION, "endpoint_summaries": endpoints, "comparisons": comparisons,
            "statistics_note": "Counts and paired deltas are descriptive. No calibrated false-positive rate, "
                               "p-value, dilution percentage, or authenticity score is claimed."}


def markdown(summary):
    lines = ["# RelayAudit report", "", f"Synthetic data: **{summary['synthetic']}** · Run: **{summary['run_status']}**",
             "", summary["limitation"], "",
             "| Endpoint alias | Attempts | Graded | Passes | API errors | Median response ms |",
             "|---|---:|---:|---:|---:|---:|"]
    for name, value in summary["endpoint_summaries"].items():
        latency = "—" if value["median_latency_ms"] is None else f"{value['median_latency_ms']:.1f}"
        lines.append(f"| {name} | {value['attempts']} | {value['graded']} | {value['passes']} | "
                     f"{sum(value['errors'].values())} | {latency} |")
    lines += ["", "## Paired comparisons", ""]
    for comparison in summary["comparisons"]:
        delta = comparison["pass_rate_delta_pp"]
        rendered = "unknown" if delta is None else f"{delta:+.2f} percentage points"
        lines.append(f"- {comparison['target']} vs {comparison['reference']}: {comparison['paired_graded']} "
                     f"jointly graded tasks; delta {rendered}; declared conditions match: {comparison['declared_conditions_match']}; "
                     f"{comparison['interpretation']}. Model identity: not verified.")
    if not summary["comparisons"]:
        lines.append("No paired reference comparison. A single endpoint cannot establish substitution.")
    lines += ["", "## Per-category counts", "", "| Endpoint | Category | Passed / graded |",
              "|---|---|---:|"]
    for name, value in summary["endpoint_summaries"].items():
        for category, result in value["categories"].items():
            lines.append(f"| {name} | {category} | {result['passes']} / {result['graded']} |")
    lines += ["", summary["statistics_note"], "",
              "Latency measures the full non-streaming HTTP response, including network and queueing. "
              "It is not TTFT, pure inference speed, or evidence of a cheaper model.", "",
              "Usage is provider-reported. It does not independently prove token billing accuracy.", ""]
    return "\n".join(lines)


def html_report(summary):
    esc = html.escape
    rows = []
    for name, value in summary["endpoint_summaries"].items():
        latency = "—" if value["median_latency_ms"] is None else f"{value['median_latency_ms']:.1f}"
        rows.append(f"<tr><td>{esc(name)}</td><td>{value['attempts']}</td><td>{value['graded']}</td>"
                    f"<td>{value['passes']}</td><td>{sum(value['errors'].values())}</td><td>{latency}</td></tr>")
    label = "SIMULATED DEMO · NOT A REAL RELAY TEST" if summary["synthetic"] else "ENDPOINT CAPABILITY AUDIT"
    comparisons = esc(json.dumps(summary["comparisons"], ensure_ascii=False, indent=2))
    return f'''<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
<title>RelayAudit evidence report</title><style>
body{{background:#f5f4f0;color:#192927;font:16px/1.6 system-ui;margin:0;padding:40px 24px}}
main{{max-width:1000px;margin:auto}}.eyebrow{{letter-spacing:.12em;font-size:12px;color:#546a60}}
h1{{font-size:44px;margin:10px 0}}.note{{background:#fff1d6;border-left:4px solid #be8738;padding:18px}}
table{{background:white;width:100%;border-collapse:collapse;margin:28px 0}}
th,td{{padding:14px;text-align:left;border-bottom:1px solid #e1e6e2}}th{{background:#e9eeea}}
pre{{overflow:auto;background:#fff;padding:20px;border:1px solid #dfe5df}}footer{{color:#64736b}}
@media(max-width:650px){{body{{padding:24px 12px}}h1{{font-size:32px}}th,td{{padding:8px;font-size:12px}}}}
</style><main><div class="eyebrow">{label}</div><h1>Observe. Compare. Reproduce.</h1>
<p class="note">{esc(summary['limitation'])}</p><p>Run status: {esc(summary['run_status'])}</p>
<table><thead><tr><th>Alias</th><th>Attempts</th><th>Graded</th><th>Passes</th><th>API errors</th>
<th>Median ms</th></tr></thead><tbody>{''.join(rows)}</tbody></table>
<h2>Paired evidence</h2><pre>{comparisons}</pre>
<footer>{esc(summary['statistics_note'])}<p>Keys and endpoint URLs are excluded. Reported model labels and usage are unverified. Full-response latency is not TTFT.</p></footer></main></html>'''
