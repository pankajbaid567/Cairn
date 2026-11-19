"""``cairn-scenario``: the scenario command line (design §12).

    cairn-scenario validate <file> [--format text|json]

Exit status: 0 the scenario passes every rule, 1 it has violations, 2 the file is
unreadable or is not valid YAML (or the command line is wrong).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from cairn_eval.scenario.loader import ScenarioLoadError, load_scenario
from cairn_eval.scenario.validator import summarize, validate
from cairn_eval.scenario.violation import Violation

EXIT_OK = 0
EXIT_VIOLATIONS = 1
EXIT_UNREADABLE = 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cairn-scenario", description="Kestrel Pay scenario tools."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("validate", help="check a scenario file against every rule")
    check.add_argument("file", help="path to scenario.yaml")
    check.add_argument("--format", choices=("text", "json"), default="text", dest="output_format")
    render = commands.add_parser("render", help="render the scenario into corpus files")
    render.add_argument("--scenario", default="scenarios/kestrel_pay/scenario.yaml")
    render.add_argument("--as-of", default=None, help="YYYY-MM-DD; default: meta.reference_date")
    render.add_argument("--out", required=True)
    render.add_argument("--overwrite", action="store_true")
    return parser


def _render_command(scenario_file: str, as_of: str | None, out: str, overwrite: bool) -> int:
    import datetime as dt
    import hashlib
    from pathlib import Path

    from cairn_eval.scenario.model import Scenario
    from cairn_eval.scenario.render import RenderError, render_corpus

    try:
        raw = load_scenario(scenario_file)
    except ScenarioLoadError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_UNREADABLE
    violations = validate(raw)
    if violations:
        print(
            f"error: {scenario_file} has {len(violations)} violation(s); fix them first",
            file=sys.stderr,
        )
        return EXIT_VIOLATIONS
    sha = hashlib.sha256(Path(scenario_file).read_bytes()).hexdigest()
    try:
        manifest = render_corpus(
            Scenario.model_validate(raw),
            Path(out),
            as_of=dt.date.fromisoformat(as_of) if as_of else None,
            overwrite=overwrite,
            scenario_sha256=sha,
        )
    except RenderError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_UNREADABLE
    rendered = sum(1 for s in manifest["sources"].values() if s["status"] == "rendered")
    print(
        f"rendered {rendered} of {len(manifest['sources'])} sources, corpus_sha256={manifest['corpus_sha256']}"  # noqa: E501
    )
    return EXIT_OK


def _violation_dict(v: Violation) -> dict[str, str]:
    return {"rule": v.rule, "path": v.path, "message": v.message}


def _validate_command(file: str, output_format: str) -> int:
    try:
        raw = load_scenario(file)
    except ScenarioLoadError as exc:
        if output_format == "json":
            error = {"message": exc.message, "line": exc.line, "column": exc.column}
            print(json.dumps({"file": file, "ok": False, "error": error}, indent=2))
        else:
            print(f"error: {exc}", file=sys.stderr)
        return EXIT_UNREADABLE
    violations = validate(raw)
    if output_format == "json":
        report: dict[str, object] = {
            "file": file,
            "ok": not violations,
            "violations": [_violation_dict(v) for v in violations],
        }
        if not violations:
            report["summary"] = summarize(raw)
        print(json.dumps(report, indent=2))
    elif violations:
        for v in violations:
            print(f"{v.rule:<6} {v.path or '(root)'}  {v.message}")
        print(f"{len(violations)} violation(s) in {file}")
    else:
        counts = ", ".join(f"{n} {name}" for name, n in summarize(raw).items())
        print(f"{file}: OK ({counts})")
    return EXIT_VIOLATIONS if violations else EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point; returns the exit status (argparse exits 2 itself on a usage error)."""
    args = _parser().parse_args(argv)
    if args.command == "render":
        return _render_command(args.scenario, args.as_of, args.out, args.overwrite)
    return _validate_command(args.file, args.output_format)


if __name__ == "__main__":
    sys.exit(main())
