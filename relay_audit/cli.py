import argparse
import json
import sys
from datetime import datetime, timezone

from .config import load, validate
from .probes import FAMILIES, build
from .runner import demo_config, plan, run


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "guard":
        from .guard.cli import main as guard_main
        return guard_main(arguments[1:])
    parser = argparse.ArgumentParser(description="RelayAudit AgentGuard continuous monitoring and automatic alerts; identity unverified")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("guard", help="Local passive Agent API gateway and automatic notifications")
    demo = commands.add_parser("demo", help="Run a clearly labeled offline simulation")
    demo.add_argument("--out")
    commands.add_parser("probes", help="List the synthetic probe families")
    for name in ("plan", "run"):
        command = commands.add_parser(name)
        command.add_argument("--config", required=True)
        command.add_argument("--allow-local-http", action="store_true", help="Loopback integration tests only")
        if name == "run":
            command.add_argument("--out")
            command.add_argument("--save-responses", action="store_true",
                                 help="Save redacted answer text locally; inspect before sharing")
    export = commands.add_parser("export-probes", help="Export reproducible prompts and local answer keys")
    export.add_argument("--seed", type=int, default=20261011)
    export.add_argument("--variants", type=int, default=3)
    args = parser.parse_args(arguments)
    try:
        if args.command == "probes":
            print("\n".join(FAMILIES))
            return 0
        if args.command == "export-probes":
            if not 0 <= args.seed < 2**32 or not 1 <= args.variants <= 100:
                raise ValueError("Invalid seed or variants")
            print(json.dumps([{"id": p.id, "family": p.family, "category": p.category,
                               "prompt": p.prompt, "expected": p.expected, "digest": p.digest,
                               "tool": p.tool} for p in build(args.seed, args.variants)], ensure_ascii=False, indent=2))
            return 0
        config = validate(demo_config()) if args.command == "demo" else load(args.config, args.allow_local_http)
        if args.command == "plan":
            print(json.dumps(plan(config), ensure_ascii=False, indent=2))
            return 0
        output = args.out or "reports/" + args.command + "-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
        print(json.dumps(plan(config), ensure_ascii=False, indent=2))

        def progress(done, total, alias, reason):
            print(f"[{done}/{total}] {alias}: {reason}", flush=True)

        summary = run(config, output, getattr(args, "save_responses", False), progress=progress)
        print(f"Report: {output}/report.html; synthetic={summary['synthetic']}; identity=not_verified")
        return 0 if summary["run_status"] == "complete" else 2
    except (ValueError, OSError, TypeError, AttributeError, KeyError) as exc:
        # Config and key errors can include user-controlled content. Avoid printing it.
        print(f"Audit stopped safely ({type(exc).__name__}). Check config, env-variable presence, "
              "request budget and a NEW output directory. No automatic retries.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
