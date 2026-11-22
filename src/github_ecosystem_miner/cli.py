from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from github_ecosystem_miner.association import mine_association_rules
from github_ecosystem_miner.cleaning import clean_run
from github_ecosystem_miner.collection import collect_enrichment, collect_search
from github_ecosystem_miner.config import load_config
from github_ecosystem_miner.errors import MinerError
from github_ecosystem_miner.runs import create_run, list_runs, load_run
from github_ecosystem_miner.storage import connect


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gem", description="Collect and mine reproducible GitHub technology ecosystems."
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path.cwd(),
        help="Project root (default: current directory)",
    )
    parser.add_argument(
        "--config", type=Path, default=Path("config.toml"), help="TOML configuration file"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="Create, list, or inspect isolated runs")
    run_commands = run.add_subparsers(dest="run_command", required=True)
    create = run_commands.add_parser("create", help="Create a run from the current configuration")
    create.add_argument("--run-id", help="Optional explicit run ID")
    run_commands.add_parser("list", help="List local runs")
    status = run_commands.add_parser("status", help="Show a run manifest and row counts")
    status.add_argument("--run-id", required=True)

    collect = commands.add_parser("collect", help="Run a resumable collection phase")
    collect_commands = collect.add_subparsers(dest="collect_command", required=True)
    search = collect_commands.add_parser(
        "search", help="Discover repositories with sliced Search API queries"
    )
    search.add_argument("--run-id", required=True)
    enrich = collect_commands.add_parser("enrich", help="Enrich repositories with batched GraphQL")
    enrich.add_argument("--run-id", required=True)

    clean = commands.add_parser("clean", help="Parse, normalize, and audit technology evidence")
    clean.add_argument("--run-id", required=True)

    mine = commands.add_parser("mine-rules", help="Mine FP-Growth rules and technology communities")
    mine.add_argument("--run-id", required=True)

    pipeline = commands.add_parser(
        "pipeline", help="Run search, enrichment, cleaning, and rule mining"
    )
    pipeline.add_argument("--run-id", help="Resume a run or create this explicit run ID")
    return parser


def _config_path(root: Path, configured: Path) -> Path:
    return configured if configured.is_absolute() else root / configured


def _show_status(context) -> None:
    manifest = context.read_manifest()
    with connect(context.database_path, read_only=True) as connection:
        counts = {
            "repositories": connection.execute(
                "SELECT COUNT(*) FROM repositories WHERE run_id = ?", [context.run_id]
            ).fetchone()[0],
            "enriched": connection.execute(
                "SELECT COUNT(*) FROM repository_enrichment WHERE run_id = ? AND status = 'ok'",
                [context.run_id],
            ).fetchone()[0],
            "kept": connection.execute(
                "SELECT COUNT(*) FROM repository_cleaning WHERE run_id = ? AND kept",
                [context.run_id],
            ).fetchone()[0],
            "evidence": connection.execute(
                "SELECT COUNT(*) FROM technology_evidence WHERE run_id = ?", [context.run_id]
            ).fetchone()[0],
            "rules": connection.execute(
                "SELECT COUNT(*) FROM association_rules WHERE run_id = ?", [context.run_id]
            ).fetchone()[0],
            "substitute_pairs": connection.execute(
                "SELECT COUNT(*) FROM technology_substitutes WHERE run_id = ?", [context.run_id]
            ).fetchone()[0],
            "api_calls": connection.execute(
                "SELECT COUNT(*) FROM api_calls WHERE run_id = ?", [context.run_id]
            ).fetchone()[0],
        }
    print(json.dumps({"manifest": manifest, "counts": counts}, indent=2, sort_keys=True))


def execute(args: argparse.Namespace) -> int:
    root = args.project_root.resolve()
    config = load_config(_config_path(root, args.config))

    if args.command == "run" and args.run_command == "create":
        context = create_run(root, config, args.run_id)
        print(context.run_id)
        return 0
    if args.command == "run" and args.run_command == "list":
        for manifest in list_runs(root):
            print(
                f"{manifest.get('run_id', '?')}\t{manifest.get('status', '?')}\t"
                f"{manifest.get('created_at', '?')}\t{manifest.get('config_hash', '')[:8]}"
            )
        return 0

    run_id = args.run_id
    if args.command == "pipeline" and run_id is None:
        context = create_run(root, config)
        run_id = context.run_id
        print(f"Created run: {run_id}")
    else:
        context = load_run(root, run_id, config)

    if args.command == "run" and args.run_command == "status":
        _show_status(context)
    elif args.command == "collect" and args.collect_command == "search":
        print(json.dumps({"repositories": collect_search(context)}, indent=2))
    elif args.command == "collect" and args.collect_command == "enrich":
        print(json.dumps({"enriched": collect_enrichment(context)}, indent=2))
    elif args.command == "clean":
        print(json.dumps(clean_run(context), indent=2))
    elif args.command == "mine-rules":
        print(json.dumps(mine_association_rules(context), indent=2))
    elif args.command == "pipeline":
        results = {
            "run_id": run_id,
            "repositories": collect_search(context),
            "enriched": collect_enrichment(context),
            "cleaning": clean_run(context),
            "mining": mine_association_rules(context),
        }
        print(json.dumps(results, indent=2))
    return 0


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        raise SystemExit(execute(args))
    except MinerError as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(2) from error


if __name__ == "__main__":
    main()
