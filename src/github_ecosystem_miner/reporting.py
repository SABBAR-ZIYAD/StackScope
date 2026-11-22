from __future__ import annotations

# The report template keeps long HTML/CSS lines readable as complete fragments.
# ruff: noqa: E501
import hashlib
import html
import json

from github_ecosystem_miner.runs import RunContext, utc_now
from github_ecosystem_miner.storage import connect

EXPORT_TABLES = (
    "search_slices",
    "repositories",
    "repository_enrichment",
    "repository_snapshots",
    "enrichment_metrics",
    "api_calls",
    "technology_evidence",
    "technology_evidence_snapshots",
    "cleaning_audit",
    "repository_cleaning",
    "frequent_itemsets",
    "association_rules",
    "mining_threshold_evaluation",
    "technology_substitutes",
    "technology_communities",
    "cluster_evaluations",
    "repository_clusters",
    "cluster_profiles",
    "robustness_results",
    "temporal_trends",
)


def generate_report(context: RunContext) -> dict[str, object]:
    context.update_stage("report", "running")
    try:
        data = _report_data(context)
        report_path = context.run_dir / "report.html"
        report_path.write_text(_render_report(context, data), encoding="utf-8")
        digest = hashlib.sha256(report_path.read_bytes()).hexdigest()
        with connect(context.database_path) as connection:
            connection.execute(
                "INSERT OR REPLACE INTO report_artifacts VALUES (?, 'html', ?, ?, now())",
                [context.run_id, report_path.name, digest],
            )
        manifest = context.read_manifest()
        manifest.setdefault("artifacts", {})["report"] = report_path.name
        manifest["updated_at"] = utc_now()
        context.write_manifest(manifest)
        result = {
            "path": str(report_path),
            "sha256": digest,
            "repositories": data["summary"]["repositories"],
            "rules": data["summary"]["rules"],
            "clusters": data["summary"]["clusters"],
        }
        context.update_stage("report", "complete", row_count=1, message=json.dumps(result))
        return result
    except Exception as error:
        context.update_stage("report", "failed", message=str(error))
        raise


def export_run(context: RunContext, file_format: str = "parquet") -> dict[str, object]:
    if file_format not in {"parquet", "csv"}:
        raise ValueError("Export format must be parquet or csv")
    export_dir = context.run_dir / "exports" / file_format
    export_dir.mkdir(parents=True, exist_ok=True)
    exported = []
    with connect(context.database_path) as connection:
        for table in EXPORT_TABLES:
            count = connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE run_id = ?", [context.run_id]
            ).fetchone()[0]
            if not count:
                continue
            path = export_dir / f"{table}.{file_format}"
            escaped_path = str(path).replace("'", "''")
            if file_format == "parquet":
                connection.execute(
                    f"COPY (SELECT * FROM {table} WHERE run_id = ?) "
                    f"TO '{escaped_path}' (FORMAT PARQUET)",
                    [context.run_id],
                )
            else:
                connection.execute(
                    f"COPY (SELECT * FROM {table} WHERE run_id = ?) "
                    f"TO '{escaped_path}' (HEADER, DELIMITER ',')",
                    [context.run_id],
                )
            exported.append({"table": table, "rows": int(count), "path": str(path)})
    return {"format": file_format, "tables": exported, "directory": str(export_dir)}


def _report_data(context: RunContext) -> dict[str, object]:
    with connect(context.database_path, read_only=True) as connection:
        summary_row = connection.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM repositories WHERE run_id = ?),
              (SELECT COUNT(*) FROM repository_enrichment WHERE run_id = ? AND status = 'ok'),
              (SELECT COUNT(*) FROM repository_cleaning WHERE run_id = ? AND kept),
              (SELECT COUNT(*) FROM technology_evidence WHERE run_id = ?),
              (SELECT COUNT(*) FROM association_rules WHERE run_id = ?),
              (SELECT COUNT(*) FROM cluster_profiles WHERE run_id = ?)
            """,
            [context.run_id] * 6,
        ).fetchone()
        summary = dict(
            zip(
                ("repositories", "enriched", "kept", "evidence", "rules", "clusters"),
                map(int, summary_row),
                strict=True,
            )
        )
        api = connection.execute(
            """
            SELECT api, resource, COUNT(*), COALESCE(SUM(bytes_transferred), 0),
                   COALESCE(SUM(graphql_cost), 0),
                   SUM(CASE WHEN status IN (403, 429) THEN 1 ELSE 0 END)
            FROM api_calls WHERE run_id = ? GROUP BY api, resource ORDER BY api, resource
            """,
            [context.run_id],
        ).fetchall()
        cleaning = connection.execute(
            """
            SELECT reason, COUNT(*) FROM repository_cleaning
            WHERE run_id = ? GROUP BY reason ORDER BY COUNT(*) DESC
            """,
            [context.run_id],
        ).fetchall()
        technologies = connection.execute(
            """
            SELECT technology, category, COUNT(DISTINCT repository_id) AS repositories
            FROM technology_evidence WHERE run_id = ?
            GROUP BY technology, category ORDER BY repositories DESC, technology LIMIT 30
            """,
            [context.run_id],
        ).fetchall()
        rules = connection.execute(
            """
            SELECT antecedent_json, consequent, support, confidence, lift
            FROM association_rules WHERE run_id = ? AND NOT redundant AND NOT same_family
            ORDER BY lift DESC, confidence DESC LIMIT 30
            """,
            [context.run_id],
        ).fetchall()
        clusters = connection.execute(
            """
            SELECT cluster_id, label, repository_count, top_technologies_json,
                   representative_repositories_json, metrics_json
            FROM cluster_profiles WHERE run_id = ? ORDER BY cluster_id
            """,
            [context.run_id],
        ).fetchall()
        robustness = connection.execute(
            """
            SELECT check_name, metric, value, details_json FROM robustness_results
            WHERE run_id = ? ORDER BY check_name
            """,
            [context.run_id],
        ).fetchall()
        temporal = connection.execute(
            """
            SELECT cohort, technology, prevalence, repository_count, caveat
            FROM temporal_trends WHERE run_id = ?
            ORDER BY cohort, prevalence DESC LIMIT 100
            """,
            [context.run_id],
        ).fetchall()
    return {
        "summary": summary,
        "api": api,
        "cleaning": cleaning,
        "technologies": technologies,
        "rules": rules,
        "clusters": clusters,
        "robustness": robustness,
        "temporal": temporal,
    }


def _render_report(context: RunContext, data: dict[str, object]) -> str:
    manifest = context.read_manifest()
    summary = data["summary"]
    cards = "".join(
        f'<div class="card"><b>{html.escape(name.title())}</b><span>{value:,}</span></div>'
        for name, value in summary.items()
    )
    cluster_sections = []
    for cluster_id, label, count, technologies, representatives, metrics in data["clusters"]:
        top = json.loads(technologies)
        reps = json.loads(representatives)
        cluster_sections.append(
            f"<article><h3>Family {cluster_id}: {html.escape(label)}</h3>"
            f"<p>{count} repositories. Representatives: {html.escape(', '.join(reps))}</p>"
            f"<p>Evidence: {html.escape(', '.join(item['technology'] for item in top[:8]))}</p>"
            f"<details><summary>Profile metrics</summary><pre>{html.escape(metrics)}</pre></details>"
            "</article>"
        )
    plot = (
        '<img src="cluster_diagnostic.svg" alt="Two-dimensional cluster diagnostic">'
        if (context.run_dir / "cluster_diagnostic.svg").exists()
        else "<p>No clustering diagnostic is available for this run.</p>"
    )
    limitations = [
        "GitHub Search and the configured query define the population; results do not represent all software.",
        "Dependency files describe the collected commit, not the repository's historical dependencies.",
        "Association does not imply causation or technical compatibility.",
        "Automated family labels summarize over-represented evidence and require human review.",
        "Small cohorts and rare technologies produce unstable estimates.",
    ]
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>StackScope report — {html.escape(context.run_id)}</title>
<style>
body{{font:15px/1.5 system-ui,sans-serif;max-width:1100px;margin:auto;padding:32px;color:#172033}}
h1,h2{{color:#0f172a}} .cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:12px}}
.card{{background:#eef2ff;border-radius:10px;padding:16px;display:flex;flex-direction:column}}.card span{{font-size:26px}}
table{{border-collapse:collapse;width:100%;margin:12px 0 28px}}th,td{{border-bottom:1px solid #ddd;padding:8px;text-align:left}}
article{{border-left:4px solid #6366f1;padding:4px 16px;margin:18px 0}}img{{max-width:100%;height:auto}}code,pre{{white-space:pre-wrap}}
</style></head><body>
<h1>StackScope: GitHub technology ecosystem</h1>
<p>Run <code>{html.escape(context.run_id)}</code>, generated {html.escape(utc_now())}.</p>
<div class="cards">{cards}</div>
<h2>Collection scope and API cost</h2>{_table(('API','Budget','Requests','Bytes','GraphQL points','Limit responses'), data['api'])}
<h2>Cleaning funnel</h2>{_table(('Decision','Repositories'), data['cleaning'])}
<h2>Technology popularity</h2>{_table(('Technology','Category','Repositories'), data['technologies'])}
<h2>Association rules</h2>{_rules_table(data['rules'])}
<h2>Project families</h2>{plot}{''.join(cluster_sections)}
<h2>Robustness checks</h2>{_table(('Check','Metric','Value','Details'), data['robustness'])}
<h2>Creation cohorts</h2><p><b>Important:</b> technologies are observed at collection time, not historically.</p>
{_table(('Cohort','Technology','Prevalence','Repositories','Caveat'), data['temporal'])}
<h2>Limitations</h2><ul>{''.join(f'<li>{html.escape(item)}</li>' for item in limitations)}</ul>
<h2>Reproducibility</h2><pre>{html.escape(json.dumps(manifest, indent=2, sort_keys=True))}</pre>
</body></html>"""


def _table(headers: tuple[str, ...], rows: object) -> str:
    body = list(rows)
    if not body:
        return "<p>No results available for this stage.</p>"
    headings = "".join(f"<th>{html.escape(header)}</th>" for header in headers)
    rendered = "".join(
        "<tr>"
        + "".join(f"<td>{html.escape(_display(value))}</td>" for value in row)
        + "</tr>"
        for row in body
    )
    return f"<table><thead><tr>{headings}</tr></thead><tbody>{rendered}</tbody></table>"


def _rules_table(rows: object) -> str:
    converted = []
    for antecedent, consequent, support, confidence, lift in rows:
        converted.append(
            (
                ", ".join(json.loads(antecedent)),
                consequent,
                support,
                confidence,
                lift,
            )
        )
    return _table(("If a project uses", "It often uses", "Support", "Confidence", "Lift"), converted)


def _display(value: object) -> str:
    if isinstance(value, float):
        return f"{value:.4f}"
    return "" if value is None else str(value)
