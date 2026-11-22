from __future__ import annotations

import json
import math
import warnings
from collections import defaultdict
from itertools import combinations

import networkx as nx
import pandas as pd
from mlxtend.frequent_patterns import association_rules, fpgrowth

from github_ecosystem_miner.errors import DataValidationError
from github_ecosystem_miner.progress import get_logger
from github_ecosystem_miner.runs import RunContext
from github_ecosystem_miner.storage import connect

LOGGER = get_logger("association")

FAMILY_PREFIXES = (
    "torch",
    "pytest",
    "sphinx",
    "tensorflow",
    "jupyter",
    "flake8",
    "mkdocs",
    "docker",
    "google",
    "azure",
    "aws",
    "langchain",
    "nvidia",
)


def mine_association_rules(context: RunContext) -> dict[str, int]:
    context.update_stage("mine_rules", "running")
    LOGGER.info("[mine-rules] Building repository-technology baskets")
    try:
        basket = _load_basket(context)
        if basket.empty or basket.shape[0] < 2:
            raise DataValidationError(
                "At least two kept repositories with technologies are required"
            )

        item_support = basket.mean(axis=0)
        basket = basket.loc[:, item_support >= context.config.mining.minimum_item_support]
        if basket.shape[1] < 2:
            raise DataValidationError("Fewer than two technologies meet minimum_item_support")

        LOGGER.info(
            "[mine-rules] Matrix: %,d repositories × %,d technologies",
            len(basket),
            basket.shape[1],
        )
        LOGGER.info("[mine-rules] Evaluating support thresholds with FP-Growth")
        threshold_rows = _evaluate_thresholds(context, basket)
        LOGGER.info(
            "[mine-rules] Mining itemsets at %.3f minimum support",
            context.config.mining.minimum_support,
        )
        frequent = fpgrowth(
            basket,
            min_support=context.config.mining.minimum_support,
            use_colnames=True,
            max_len=context.config.mining.maximum_itemset_length,
        )
        if frequent.empty:
            rules = pd.DataFrame()
        else:
            rules = _generate_rules(frequent, len(basket), context.config.mining.minimum_confidence)
            rules = rules[
                (rules["lift"] >= context.config.mining.minimum_lift)
                & (rules["consequents"].apply(len) == 1)
            ].copy()
            if not rules.empty:
                rules["consequent"] = rules["consequents"].apply(lambda values: next(iter(values)))
                rules["redundant"] = _redundancy_flags(
                    rules, context.config.mining.redundancy_improvement
                )
                rules["same_family"] = rules.apply(_same_family_rule, axis=1)

        _save_results(context, basket, frequent, rules)
        communities = _save_communities(context, rules)
        substitutes = _save_substitutes(context, basket)
        result = {
            "repositories": len(basket),
            "technologies": basket.shape[1],
            "frequent_itemsets": len(frequent),
            "rules": len(rules),
            "non_redundant_rules": int((~rules["redundant"]).sum()) if not rules.empty else 0,
            "communities": communities,
            "thresholds_evaluated": threshold_rows,
            "substitute_pairs": substitutes,
        }
        context.update_stage(
            "mine_rules", "complete", row_count=result["rules"], message=json.dumps(result)
        )
        LOGGER.info(
            "[mine-rules] Complete: %,d itemsets, %,d rules, %,d communities",
            len(frequent),
            len(rules),
            communities,
        )
        return result
    except Exception as error:
        context.update_stage("mine_rules", "failed", message=str(error))
        raise


def _load_basket(context: RunContext) -> pd.DataFrame:
    with connect(context.database_path, read_only=True) as connection:
        rows = connection.execute(
            """
            SELECT e.repository_id, e.technology
            FROM technology_evidence e
            JOIN repository_cleaning c
              ON c.run_id = e.run_id AND c.repository_id = e.repository_id
            WHERE e.run_id = ? AND c.kept = TRUE
              AND e.directness <> 'environment_snapshot'
            GROUP BY e.repository_id, e.technology
            ORDER BY e.repository_id, e.technology
            """,
            [context.run_id],
        ).fetchall()
    if not rows:
        return pd.DataFrame()
    frame = pd.DataFrame(rows, columns=["repository_id", "technology"])
    return (
        frame.assign(present=True)
        .pivot_table(
            index="repository_id",
            columns="technology",
            values="present",
            aggfunc="any",
            fill_value=False,
        )
        .astype(bool)
    )


def _redundancy_flags(rules: pd.DataFrame, improvement: float) -> list[bool]:
    by_consequent: dict[str, list[tuple[frozenset[str], float]]] = defaultdict(list)
    for row in rules.itertuples():
        by_consequent[row.consequent].append((frozenset(row.antecedents), float(row.confidence)))
    flags = []
    for row in rules.itertuples():
        antecedent = frozenset(row.antecedents)
        best_subset = max(
            (
                confidence
                for subset, confidence in by_consequent[row.consequent]
                if subset < antecedent
            ),
            default=0.0,
        )
        flags.append(bool(best_subset and row.confidence <= best_subset * (1 + improvement)))
    return flags


def _evaluate_thresholds(context: RunContext, basket: pd.DataFrame) -> int:
    thresholds = sorted(
        set(context.config.mining.support_grid) | {context.config.mining.minimum_support},
        reverse=True,
    )
    rows: list[tuple[float, int, int, int, int]] = []
    for threshold in thresholds:
        frequent = fpgrowth(
            basket,
            min_support=threshold,
            use_colnames=True,
            max_len=context.config.mining.maximum_itemset_length,
        )
        if frequent.empty:
            confidence_count = 0
            lift_count = 0
        else:
            rules = _generate_rules(frequent, len(basket), context.config.mining.minimum_confidence)
            confidence_count = len(rules)
            lift_count = int((rules["lift"] >= context.config.mining.minimum_lift).sum())
        rows.append(
            (
                threshold,
                math.ceil(threshold * len(basket)),
                len(frequent),
                confidence_count,
                lift_count,
            )
        )
    with connect(context.database_path) as connection:
        connection.execute(
            "DELETE FROM mining_threshold_evaluation WHERE run_id = ?", [context.run_id]
        )
        for row in rows:
            connection.execute(
                "INSERT INTO mining_threshold_evaluation VALUES (?, ?, ?, ?, ?, ?)",
                [context.run_id, *row],
            )
    return len(rows)


def _generate_rules(
    frequent: pd.DataFrame, transaction_count: int, minimum_confidence: float
) -> pd.DataFrame:
    # mlxtend computes optional certainty metrics internally and warns when a
    # consequent is universal. Those NaNs do not affect the metrics we store.
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore", message="invalid value encountered in divide", category=RuntimeWarning
        )
        return association_rules(
            frequent,
            num_itemsets=transaction_count,
            metric="confidence",
            min_threshold=minimum_confidence,
        )


def _family(name: str) -> str:
    clean = name.removeprefix("lang:")
    for prefix in FAMILY_PREFIXES:
        if clean == prefix or clean.startswith(prefix + "-") or clean.endswith("-" + prefix):
            return prefix
    return clean.split("-", 1)[0]


def _same_family_rule(row) -> bool:
    consequent_family = _family(row["consequent"])
    return all(_family(item) == consequent_family for item in row["antecedents"])


def _save_results(
    context: RunContext,
    basket: pd.DataFrame,
    frequent: pd.DataFrame,
    rules: pd.DataFrame,
) -> None:
    with connect(context.database_path) as connection:
        connection.execute("DELETE FROM frequent_itemsets WHERE run_id = ?", [context.run_id])
        connection.execute("DELETE FROM association_rules WHERE run_id = ?", [context.run_id])
        for row in frequent.itertuples():
            items = sorted(row.itemsets)
            connection.execute(
                """
                INSERT INTO frequent_itemsets
                    (run_id, items_json, item_count, support, support_count)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    context.run_id,
                    json.dumps(items),
                    len(items),
                    float(row.support),
                    int(round(row.support * len(basket))),
                ],
            )
        for row in rules.itertuples():
            conviction = float(row.conviction)
            connection.execute(
                """
                INSERT INTO association_rules
                    (run_id, antecedent_json, consequent, support, support_count,
                     confidence, lift, leverage, conviction, antecedent_count,
                     redundant, same_family)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    context.run_id,
                    json.dumps(sorted(row.antecedents)),
                    row.consequent,
                    float(row.support),
                    int(round(row.support * len(basket))),
                    float(row.confidence),
                    float(row.lift),
                    float(row.leverage),
                    conviction if math.isfinite(conviction) else None,
                    len(row.antecedents),
                    bool(row.redundant),
                    bool(row.same_family),
                ],
            )


def _save_communities(context: RunContext, rules: pd.DataFrame) -> int:
    graph = nx.Graph()
    if not rules.empty:
        selected = rules[
            (~rules["redundant"])
            & (~rules["same_family"])
            & (rules["lift"] >= context.config.mining.community_minimum_lift)
        ].sort_values(["lift", "consequent"], ascending=[False, True])
        for row in selected.itertuples():
            for antecedent in sorted(row.antecedents):
                previous = graph.get_edge_data(antecedent, row.consequent, {}).get("weight", 0.0)
                graph.add_edge(antecedent, row.consequent, weight=max(previous, float(row.lift)))
    communities = (
        list(
            nx.community.louvain_communities(
                graph, weight="weight", seed=context.config.mining.random_seed
            )
        )
        if graph.number_of_edges()
        else []
    )
    communities = sorted(communities, key=lambda group: (-len(group), sorted(group)))
    with connect(context.database_path) as connection:
        connection.execute("DELETE FROM technology_communities WHERE run_id = ?", [context.run_id])
        for community_id, members in enumerate(communities):
            for technology in sorted(members):
                connection.execute(
                    "INSERT INTO technology_communities VALUES (?, ?, ?)",
                    [context.run_id, technology, community_id],
                )
    return len(communities)


def _save_substitutes(context: RunContext, basket: pd.DataFrame) -> int:
    supports = basket.mean(axis=0)
    candidates = []
    for technology_a, technology_b in combinations(sorted(basket.columns), 2):
        support_a = float(supports[technology_a])
        support_b = float(supports[technology_b])
        joint_count = int((basket[technology_a] & basket[technology_b]).sum())
        if joint_count == 0:
            continue
        joint_support = joint_count / len(basket)
        lift = joint_support / (support_a * support_b)
        if lift < 1.0:
            candidates.append(
                (
                    technology_a,
                    technology_b,
                    support_a,
                    support_b,
                    joint_support,
                    joint_count,
                    lift,
                )
            )
    candidates.sort(key=lambda row: (row[-1], -row[-2], row[0], row[1]))
    candidates = candidates[:1000]
    with connect(context.database_path) as connection:
        connection.execute("DELETE FROM technology_substitutes WHERE run_id = ?", [context.run_id])
        for row in candidates:
            connection.execute(
                "INSERT INTO technology_substitutes VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [context.run_id, *row],
            )
    return len(candidates)
