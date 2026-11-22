from __future__ import annotations

import json
from collections import defaultdict

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

from github_ecosystem_miner.association import _load_basket
from github_ecosystem_miner.errors import DataValidationError
from github_ecosystem_miner.runs import RunContext
from github_ecosystem_miner.storage import connect

TEMPORAL_CAVEAT = (
    "Cohorts use repository creation time, but technologies are observed at collection time; "
    "this is not historical dependency evidence."
)


def analyze_robustness(context: RunContext) -> dict[str, object]:
    context.update_stage("robustness", "running")
    try:
        basket = _load_basket(context)
        if len(basket) < 3:
            raise DataValidationError("Robustness analysis requires at least three repositories")
        with connect(context.database_path) as connection:
            connection.execute("DELETE FROM robustness_results WHERE run_id = ?", [context.run_id])
            connection.execute("DELETE FROM temporal_trends WHERE run_id = ?", [context.run_id])
        rule_result = _bootstrap_rule_stability(context, basket)
        threshold_result = _threshold_sensitivity(context)
        owner_result = _owner_cap_sensitivity(context, basket)
        star_result = _star_cohort_sensitivity(context, basket)
        temporal_rows = _temporal_cohorts(context, basket)
        temporal_rows += _snapshot_technology_trends(context)
        snapshot_result = _snapshot_coverage(context)
        cluster_result = _cluster_stability(context)
        result = {
            "repositories": len(basket),
            "bootstrap_rule_stability": rule_result,
            "threshold_sensitivity": threshold_result,
            "owner_cap_sensitivity": owner_result,
            "star_cohort_sensitivity": star_result,
            "temporal_rows": temporal_rows,
            "snapshot_coverage": snapshot_result,
            "cluster_stability": cluster_result,
        }
        context.update_stage(
            "robustness", "complete", row_count=temporal_rows, message=json.dumps(result)
        )
        return result
    except Exception as error:
        context.update_stage("robustness", "failed", message=str(error))
        raise


def _bootstrap_rule_stability(context: RunContext, basket: pd.DataFrame) -> dict[str, object]:
    with connect(context.database_path, read_only=True) as connection:
        rows = connection.execute(
            """
            SELECT antecedent_json, consequent FROM association_rules
            WHERE run_id = ? AND NOT redundant AND NOT same_family
            ORDER BY lift DESC, confidence DESC
            """,
            [context.run_id],
        ).fetchall()
    rules = [(tuple(json.loads(row[0])), str(row[1])) for row in rows]
    if not rules:
        result = {"rules": 0, "stable_rules": 0, "mean_frequency": None}
        _save_result(context, "bootstrap_rules", "stable_fraction", None, result)
        return result
    generator = np.random.default_rng(context.config.robustness.random_seed)
    hits = defaultdict(int)
    values = basket.to_numpy(dtype=bool)
    column_index = {column: index for index, column in enumerate(basket.columns)}
    valid_rules = [
        rule
        for rule in rules
        if rule[1] in column_index and all(item in column_index for item in rule[0])
    ]
    for _ in range(context.config.robustness.bootstrap_iterations):
        sample = values[generator.integers(0, len(values), size=len(values))]
        for antecedent, consequent in valid_rules:
            antecedent_mask = np.all(
                sample[:, [column_index[item] for item in antecedent]], axis=1
            )
            consequent_mask = sample[:, column_index[consequent]]
            joint = float(np.mean(antecedent_mask & consequent_mask))
            antecedent_support = float(np.mean(antecedent_mask))
            consequent_support = float(np.mean(consequent_mask))
            confidence = joint / antecedent_support if antecedent_support else 0.0
            lift = confidence / consequent_support if consequent_support else 0.0
            if (
                joint >= context.config.mining.minimum_support
                and confidence >= context.config.mining.minimum_confidence
                and lift >= context.config.mining.minimum_lift
            ):
                hits[(antecedent, consequent)] += 1
    frequencies = {
        f"{', '.join(antecedent)} -> {consequent}": hits[(antecedent, consequent)]
        / context.config.robustness.bootstrap_iterations
        for antecedent, consequent in valid_rules
    }
    threshold = context.config.robustness.stable_rule_frequency
    stable = {name: value for name, value in frequencies.items() if value >= threshold}
    result = {
        "rules": len(valid_rules),
        "stable_rules": len(stable),
        "mean_frequency": float(np.mean(list(frequencies.values()))) if frequencies else None,
        "stable": dict(sorted(stable.items(), key=lambda item: (-item[1], item[0]))[:50]),
    }
    fraction = len(stable) / len(valid_rules) if valid_rules else None
    _save_result(context, "bootstrap_rules", "stable_fraction", fraction, result)
    return result


def _threshold_sensitivity(context: RunContext) -> dict[str, object]:
    with connect(context.database_path, read_only=True) as connection:
        rows = connection.execute(
            """
            SELECT minimum_support, lift_rules FROM mining_threshold_evaluation
            WHERE run_id = ? ORDER BY minimum_support DESC
            """,
            [context.run_id],
        ).fetchall()
    counts = np.array([int(row[1]) for row in rows], dtype=float)
    coefficient = float(counts.std() / counts.mean()) if len(counts) and counts.mean() else None
    details = {"grid": [{"support": row[0], "rules": row[1]} for row in rows]}
    _save_result(context, "support_thresholds", "rule_count_cv", coefficient, details)
    return {"rule_count_cv": coefficient, **details}


def _owner_cap_sensitivity(
    context: RunContext, basket: pd.DataFrame
) -> dict[str, float | int | None]:
    with connect(context.database_path, read_only=True) as connection:
        owners = connection.execute(
            """
            SELECT repository_id, owner, stars FROM repositories
            WHERE run_id = ? ORDER BY owner, stars DESC, repository_id
            """,
            [context.run_id],
        ).fetchall()
    kept = []
    counts = defaultdict(int)
    for repository_id, owner, _ in owners:
        if int(repository_id) not in basket.index:
            continue
        if counts[str(owner)] < context.config.clustering.owner_repository_cap:
            kept.append(int(repository_id))
            counts[str(owner)] += 1
    capped = basket.loc[basket.index.intersection(kept)]
    correlation = None
    if len(capped) >= 2 and basket.shape[1] >= 2:
        baseline_prevalence = basket.mean().to_numpy()
        capped_prevalence = capped.mean().to_numpy()
        if np.array_equal(baseline_prevalence, capped_prevalence):
            correlation = 1.0
        else:
            statistic = float(spearmanr(baseline_prevalence, capped_prevalence).statistic)
            correlation = statistic if np.isfinite(statistic) else None
    result = {"repositories_after_cap": len(capped), "prevalence_spearman": correlation}
    _save_result(context, "owner_cap", "prevalence_spearman", correlation, result)
    return result


def _star_cohort_sensitivity(
    context: RunContext, basket: pd.DataFrame
) -> dict[str, float | int]:
    with connect(context.database_path, read_only=True) as connection:
        rows = connection.execute(
            "SELECT repository_id, stars FROM repositories WHERE run_id = ?",
            [context.run_id],
        ).fetchall()
    stars = pd.Series({int(repository_id): int(value) for repository_id, value in rows})
    stars = stars.reindex(basket.index)
    cutoff = float(stars.quantile(context.config.robustness.star_quantile))
    high = basket.loc[stars[stars >= cutoff].index]
    medium = basket.loc[stars[stars < cutoff].index]
    high_top = set(high.mean().nlargest(min(20, basket.shape[1])).index) if len(high) else set()
    medium_top = (
        set(medium.mean().nlargest(min(20, basket.shape[1])).index) if len(medium) else set()
    )
    union = high_top | medium_top
    jaccard = len(high_top & medium_top) / len(union) if union else 1.0
    result = {
        "star_cutoff": cutoff,
        "high_repositories": len(high),
        "medium_repositories": len(medium),
        "top_technology_jaccard": jaccard,
    }
    _save_result(context, "star_cohorts", "top_technology_jaccard", jaccard, result)
    return result


def _temporal_cohorts(context: RunContext, basket: pd.DataFrame) -> int:
    with connect(context.database_path, read_only=True) as connection:
        rows = connection.execute(
            "SELECT repository_id, created_at FROM repositories WHERE run_id = ?",
            [context.run_id],
        ).fetchall()
    dates = pd.Series(
        {
            int(repository_id): pd.to_datetime(created, utc=True, errors="coerce")
            for repository_id, created in rows
        }
    ).reindex(basket.index)
    valid = dates.dropna()
    if valid.empty:
        return 0
    cutoff = valid.median()
    cohorts = {
        f"created_before_{cutoff.year}": valid[valid < cutoff].index,
        f"created_since_{cutoff.year}": valid[valid >= cutoff].index,
    }
    inserted = 0
    with connect(context.database_path) as connection:
        for cohort, indexes in cohorts.items():
            subset = basket.loc[basket.index.intersection(indexes)]
            if subset.empty:
                continue
            for technology, prevalence in subset.mean().nlargest(min(50, basket.shape[1])).items():
                connection.execute(
                    "INSERT INTO temporal_trends VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        context.run_id,
                        cohort,
                        technology,
                        float(prevalence),
                        len(subset),
                        TEMPORAL_CAVEAT,
                    ],
                )
                inserted += 1
    return inserted


def _snapshot_coverage(context: RunContext) -> dict[str, int | bool]:
    with connect(context.database_path, read_only=True) as connection:
        row = connection.execute(
            """
            SELECT COUNT(DISTINCT repository_id), COUNT(*) FROM repository_snapshots
            WHERE run_id = ?
            """,
            [context.run_id],
        ).fetchone()
    repositories, snapshots = int(row[0]), int(row[1])
    result = {
        "repositories": repositories,
        "snapshots": snapshots,
        "multiple_snapshots_available": snapshots > repositories,
    }
    _save_result(
        context,
        "snapshot_coverage",
        "snapshots_per_repository",
        snapshots / repositories if repositories else None,
        result,
    )
    return result


def _snapshot_technology_trends(context: RunContext) -> int:
    with connect(context.database_path, read_only=True) as connection:
        rows = connection.execute(
            """
            SELECT repository_id, commit_sha, technology, observed_at
            FROM technology_evidence_snapshots WHERE run_id = ?
            ORDER BY repository_id, observed_at, commit_sha, technology
            """,
            [context.run_id],
        ).fetchall()
    by_repository: dict[int, dict[str, dict[str, object]]] = defaultdict(dict)
    for repository_id, commit_sha, technology, observed_at in rows:
        snapshot = by_repository[int(repository_id)].setdefault(
            str(commit_sha), {"observed_at": observed_at, "technologies": set()}
        )
        snapshot["technologies"].add(str(technology))
    comparable = {
        repository_id: sorted(
            snapshots.values(), key=lambda item: str(item["observed_at"])
        )
        for repository_id, snapshots in by_repository.items()
        if len(snapshots) >= 2
    }
    if not comparable:
        return 0
    previous_counts = defaultdict(int)
    latest_counts = defaultdict(int)
    for snapshots in comparable.values():
        for technology in snapshots[-2]["technologies"]:
            previous_counts[technology] += 1
        for technology in snapshots[-1]["technologies"]:
            latest_counts[technology] += 1
    total = len(comparable)
    inserted = 0
    with connect(context.database_path) as connection:
        for cohort, counts in (
            ("previous_collected_snapshot", previous_counts),
            ("latest_collected_snapshot", latest_counts),
        ):
            for technology, count in sorted(counts.items(), key=lambda item: (-item[1], item[0])):
                connection.execute(
                    "INSERT OR REPLACE INTO temporal_trends VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        context.run_id,
                        cohort,
                        technology,
                        count / total,
                        total,
                        (
                            "Compares observed collected commits only; "
                            "commit dates are not normalized."
                        ),
                    ],
                )
                inserted += 1
    return inserted


def _cluster_stability(context: RunContext) -> dict[str, object]:
    with connect(context.database_path, read_only=True) as connection:
        row = connection.execute(
            """
            SELECT method, weighting, cluster_count, silhouette, stability, balance
            FROM cluster_evaluations WHERE run_id = ? AND selected
            """,
            [context.run_id],
        ).fetchone()
    if not row:
        result = {"available": False}
        _save_result(context, "cluster_stability", "stability", None, result)
        return result
    result = {
        "available": True,
        "method": row[0],
        "weighting": row[1],
        "clusters": row[2],
        "silhouette": row[3],
        "stability": row[4],
        "balance": row[5],
    }
    _save_result(context, "cluster_stability", "stability", float(row[4]), result)
    return result


def _save_result(
    context: RunContext,
    check_name: str,
    metric: str,
    value: float | None,
    details: object,
) -> None:
    with connect(context.database_path) as connection:
        connection.execute(
            "INSERT OR REPLACE INTO robustness_results VALUES (?, ?, ?, ?, ?)",
            [context.run_id, check_name, metric, value, json.dumps(details, sort_keys=True)],
        )
