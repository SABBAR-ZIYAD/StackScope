from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.cluster import AgglomerativeClustering, KMeans
from sklearn.decomposition import TruncatedSVD
from sklearn.feature_extraction.text import TfidfTransformer
from sklearn.metrics import adjusted_rand_score, silhouette_score

from github_ecosystem_miner.errors import DataValidationError
from github_ecosystem_miner.progress import get_logger
from github_ecosystem_miner.runs import RunContext
from github_ecosystem_miner.storage import connect

LOGGER = get_logger("clustering")

try:
    from sklearn.cluster import HDBSCAN
except ImportError:  # pragma: no cover - supported sklearn releases provide it
    HDBSCAN = None


CATEGORY_WEIGHTS = {"language": 0.55, "tooling": 0.8, "package": 1.0}


@dataclass
class Candidate:
    method: str
    weighting: str
    cluster_count: int
    labels: np.ndarray
    coordinates: np.ndarray
    silhouette: float
    stability: float
    balance: float
    score: float
    membership: np.ndarray


def cluster_repositories(context: RunContext) -> dict[str, object]:
    context.update_stage("cluster", "running")
    LOGGER.info("[cluster] Loading cleaned repository features")
    try:
        basket, metadata, categories = _load_features(context)
        if len(basket) < 3 or basket.shape[1] < 2:
            raise DataValidationError(
                "Clustering requires at least three kept repositories and two technologies"
            )
        candidates: list[Candidate] = []
        for weighting, matrix in _weighted_matrices(basket, categories).items():
            LOGGER.info("[cluster] Evaluating %s feature weighting", weighting)
            coordinates = _reduce(matrix, context.config.clustering.svd_components)
            candidates.extend(_evaluate_models(context, coordinates, weighting))
        if not candidates:
            raise DataValidationError("No clustering candidate produced at least two groups")
        selected = max(
            candidates,
            key=lambda candidate: (
                candidate.score,
                candidate.stability,
                candidate.silhouette,
                -candidate.cluster_count,
            ),
        )
        LOGGER.info(
            "[cluster] Selected %s/%s with %d clusters (silhouette %.3f, stability %.3f)",
            selected.method,
            selected.weighting,
            selected.cluster_count,
            selected.silhouette,
            selected.stability,
        )
        _save_evaluations(context, candidates, selected)
        profiles = _save_assignments_and_profiles(context, basket, metadata, selected)
        plot_path = _write_diagnostic_svg(context, metadata, selected)
        result = {
            "repositories": len(basket),
            "technologies": basket.shape[1],
            "candidates": len(candidates),
            "selected_method": selected.method,
            "selected_weighting": selected.weighting,
            "clusters": selected.cluster_count,
            "silhouette": round(selected.silhouette, 4),
            "stability": round(selected.stability, 4),
            "profiles": profiles,
            "diagnostic_plot": str(plot_path.relative_to(context.run_dir)),
        }
        context.update_stage(
            "cluster", "complete", row_count=selected.cluster_count, message=json.dumps(result)
        )
        LOGGER.info("[cluster] Complete: diagnostic saved to %s", plot_path)
        return result
    except Exception as error:
        context.update_stage("cluster", "failed", message=str(error))
        raise


def _load_features(
    context: RunContext,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, str]]:
    with connect(context.database_path, read_only=True) as connection:
        rows = connection.execute(
            """
            SELECT r.repository_id, r.full_name, r.owner, r.stars, r.created_at,
                   r.updated_at, r.pushed_at, e.technology, e.category
            FROM repositories r
            JOIN repository_cleaning c
              ON c.run_id = r.run_id AND c.repository_id = r.repository_id AND c.kept
            JOIN technology_evidence e
              ON e.run_id = r.run_id AND e.repository_id = r.repository_id
            WHERE r.run_id = ? AND e.directness <> 'environment_snapshot'
            ORDER BY r.stars DESC, r.repository_id, e.technology
            """,
            [context.run_id],
        ).fetchall()
    if not rows:
        return pd.DataFrame(), pd.DataFrame(), {}
    frame = pd.DataFrame(
        rows,
        columns=[
            "repository_id",
            "full_name",
            "owner",
            "stars",
            "created_at",
            "updated_at",
            "pushed_at",
            "technology",
            "category",
        ],
    )
    metadata = frame.drop_duplicates("repository_id").set_index("repository_id")
    owner_rank = metadata.groupby("owner")["stars"].rank(method="first", ascending=False)
    metadata = metadata[
        owner_rank <= context.config.clustering.owner_repository_cap
    ].copy()
    counts = frame.groupby("repository_id")["technology"].nunique()
    eligible = counts[counts >= context.config.clustering.minimum_technologies].index
    metadata = metadata.loc[metadata.index.intersection(eligible)].sort_index()
    frame = frame[frame["repository_id"].isin(metadata.index)]
    basket = (
        frame.assign(value=1.0)
        .pivot_table(
            index="repository_id",
            columns="technology",
            values="value",
            aggfunc="max",
            fill_value=0.0,
        )
        .reindex(metadata.index)
        .astype(float)
    )
    categories = (
        frame.drop_duplicates("technology").set_index("technology")["category"].to_dict()
    )
    return basket, metadata, categories


def _weighted_matrices(
    basket: pd.DataFrame, categories: dict[str, str]
) -> dict[str, np.ndarray]:
    binary = basket.to_numpy(dtype=float)
    tfidf = TfidfTransformer(norm="l2", use_idf=True).fit_transform(binary).toarray()
    weights = np.array(
        [CATEGORY_WEIGHTS.get(categories.get(column, ""), 0.7) for column in basket.columns]
    )
    category_aware = binary * weights
    norms = np.linalg.norm(category_aware, axis=1, keepdims=True)
    category_aware = category_aware / np.where(norms == 0, 1, norms)
    return {"binary": binary, "tfidf": tfidf, "category_aware": category_aware}


def _reduce(matrix: np.ndarray, requested_components: int) -> np.ndarray:
    maximum = min(matrix.shape[0] - 1, matrix.shape[1] - 1, requested_components)
    if maximum < 2:
        return matrix
    return TruncatedSVD(n_components=maximum, random_state=42).fit_transform(matrix)


def _evaluate_models(
    context: RunContext, coordinates: np.ndarray, weighting: str
) -> list[Candidate]:
    config = context.config.clustering
    distinct_points = len(np.unique(np.round(coordinates, decimals=12), axis=0))
    maximum = min(config.maximum_clusters, len(coordinates) - 1, distinct_points)
    candidates: list[Candidate] = []
    for clusters in range(config.minimum_clusters, maximum + 1):
        labels_by_seed = [
            KMeans(n_clusters=clusters, random_state=seed, n_init=10).fit_predict(coordinates)
            for seed in config.stability_seeds
        ]
        labels = labels_by_seed[0]
        stability_scores = [
            adjusted_rand_score(labels, other) for other in labels_by_seed[1:]
        ]
        stability = float(np.mean(stability_scores)) if stability_scores else 1.0
        candidates.append(
            _candidate("kmeans", weighting, labels, coordinates, stability)
        )
        hierarchical = AgglomerativeClustering(n_clusters=clusters).fit_predict(coordinates)
        feature_stability = _feature_perturbation_stability(
            coordinates, hierarchical, clusters, config.random_seed
        )
        candidates.append(
            _candidate(
                "hierarchical", weighting, hierarchical, coordinates, feature_stability
            )
        )
    if HDBSCAN is not None and len(coordinates) >= 5:
        model = HDBSCAN(
            min_cluster_size=max(2, len(coordinates) // 10),
            allow_single_cluster=False,
            copy=True,
        )
        labels = model.fit_predict(coordinates)
        groups = {int(value) for value in labels if value >= 0}
        if len(groups) >= 2:
            membership = np.asarray(getattr(model, "probabilities_", np.ones(len(labels))))
            candidates.append(
                _candidate(
                    "hdbscan",
                    weighting,
                    labels,
                    coordinates,
                    1.0 - float((labels < 0).mean()),
                    membership,
                )
            )
    return candidates


def _feature_perturbation_stability(
    coordinates: np.ndarray, baseline: np.ndarray, clusters: int, seed: int
) -> float:
    if coordinates.shape[1] < 2:
        return 1.0
    generator = np.random.default_rng(seed)
    scores = []
    width = max(2, math.ceil(coordinates.shape[1] * 0.8))
    for _ in range(5):
        columns = generator.choice(coordinates.shape[1], size=width, replace=False)
        labels = AgglomerativeClustering(n_clusters=clusters).fit_predict(
            coordinates[:, columns]
        )
        scores.append(adjusted_rand_score(baseline, labels))
    return float(np.mean(scores))


def _candidate(
    method: str,
    weighting: str,
    labels: np.ndarray,
    coordinates: np.ndarray,
    stability: float,
    membership: np.ndarray | None = None,
) -> Candidate:
    valid = labels >= 0
    cluster_values, counts = np.unique(labels[valid], return_counts=True)
    cluster_count = len(cluster_values)
    silhouette = (
        float(silhouette_score(coordinates[valid], labels[valid]))
        if cluster_count >= 2 and valid.sum() > cluster_count
        else -1.0
    )
    balance = float(counts.min() / counts.max()) if len(counts) else 0.0
    score = 0.5 * ((silhouette + 1) / 2) + 0.3 * max(0.0, stability) + 0.2 * balance
    return Candidate(
        method,
        weighting,
        cluster_count,
        labels,
        coordinates,
        silhouette,
        stability,
        balance,
        score,
        membership if membership is not None else np.ones(len(labels)),
    )


def _save_evaluations(
    context: RunContext, candidates: list[Candidate], selected: Candidate
) -> None:
    with connect(context.database_path) as connection:
        connection.execute("DELETE FROM cluster_evaluations WHERE run_id = ?", [context.run_id])
        for candidate in candidates:
            connection.execute(
                """
                INSERT INTO cluster_evaluations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    context.run_id,
                    candidate.method,
                    candidate.weighting,
                    candidate.cluster_count,
                    candidate.silhouette,
                    candidate.stability,
                    candidate.balance,
                    candidate.score,
                    candidate is selected,
                    json.dumps({"noise_count": int((candidate.labels < 0).sum())}),
                ],
            )


def _save_assignments_and_profiles(
    context: RunContext,
    basket: pd.DataFrame,
    metadata: pd.DataFrame,
    selected: Candidate,
) -> int:
    labels = selected.labels
    centers = {
        int(cluster): selected.coordinates[labels == cluster].mean(axis=0)
        for cluster in np.unique(labels)
        if cluster >= 0
    }
    distances = np.array(
        [
            float(np.linalg.norm(point - centers[int(label)])) if label >= 0 else math.nan
            for point, label in zip(selected.coordinates, labels, strict=True)
        ]
    )
    overall = basket.mean(axis=0)
    with connect(context.database_path) as connection:
        connection.execute("DELETE FROM repository_clusters WHERE run_id = ?", [context.run_id])
        connection.execute("DELETE FROM cluster_profiles WHERE run_id = ?", [context.run_id])
        for position, repository_id in enumerate(basket.index):
            connection.execute(
                "INSERT INTO repository_clusters VALUES (?, ?, ?, ?, ?)",
                [
                    context.run_id,
                    int(repository_id),
                    int(labels[position]),
                    float(selected.membership[position]),
                    None if math.isnan(distances[position]) else distances[position],
                ],
            )
        for cluster in centers:
            mask = labels == cluster
            members = basket.loc[mask]
            prevalence = members.mean(axis=0)
            ratios = (prevalence / overall.replace(0, np.nan)).sort_values(ascending=False)
            top = [
                {
                    "technology": technology,
                    "prevalence": round(float(prevalence[technology]), 4),
                    "overrepresentation": round(float(ratio), 4),
                }
                for technology, ratio in ratios.head(10).items()
            ]
            member_positions = np.where(mask)[0]
            nearest = sorted(member_positions, key=lambda pos: distances[pos])[
                : context.config.clustering.representative_count
            ]
            representatives = [str(metadata.iloc[position]["full_name"]) for position in nearest]
            cluster_metadata = metadata.iloc[member_positions]
            label = " / ".join(item["technology"] for item in top[:3]) or f"Cluster {cluster}"
            metrics = {
                "median_stars": float(cluster_metadata["stars"].median()),
                "median_created_year": _median_year(cluster_metadata["created_at"]),
                "median_pushed_year": _median_year(cluster_metadata["pushed_at"]),
                "mean_technologies": float(members.sum(axis=1).mean()),
            }
            connection.execute(
                "INSERT INTO cluster_profiles VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    context.run_id,
                    cluster,
                    label,
                    len(members),
                    json.dumps(top, sort_keys=True),
                    json.dumps(representatives),
                    json.dumps(metrics, sort_keys=True),
                ],
            )
    return len(centers)


def _median_year(values: pd.Series) -> float | None:
    parsed = pd.to_datetime(values, utc=True, errors="coerce").dropna()
    return float(parsed.dt.year.median()) if not parsed.empty else None


def _write_diagnostic_svg(
    context: RunContext, metadata: pd.DataFrame, selected: Candidate
) -> Path:
    coordinates = selected.coordinates
    x = coordinates[:, 0]
    y = coordinates[:, 1] if coordinates.shape[1] > 1 else np.zeros(len(x))
    x_scaled = _scale(x, 40, 760)
    y_scaled = _scale(y, 540, 40)
    colors = ["#2563eb", "#16a34a", "#dc2626", "#9333ea", "#ea580c", "#0891b2"]
    circles = []
    for index, (x_value, y_value, label) in enumerate(
        zip(x_scaled, y_scaled, selected.labels, strict=True)
    ):
        color = "#94a3b8" if label < 0 else colors[int(label) % len(colors)]
        name = str(metadata.iloc[index]["full_name"])
        circles.append(
            f'<circle cx="{x_value:.1f}" cy="{y_value:.1f}" r="5" fill="{color}">'
            f"<title>{name}</title></circle>"
        )
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="800" height="580" '
        'viewBox="0 0 800 580"><rect width="100%" height="100%" fill="white"/>'
        '<text x="20" y="24" font-family="sans-serif" font-size="16">'
        f"GitHub Ecosystem Miner project families — {selected.method}, {selected.weighting}</text>"
        + "".join(circles)
        + "</svg>"
    )
    path = context.run_dir / "cluster_diagnostic.svg"
    path.write_text(svg, encoding="utf-8")
    return path


def _scale(values: np.ndarray, low: float, high: float) -> np.ndarray:
    minimum, maximum = float(values.min()), float(values.max())
    if minimum == maximum:
        return np.full(len(values), (low + high) / 2)
    return low + (values - minimum) * (high - low) / (maximum - minimum)
