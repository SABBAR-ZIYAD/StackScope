# Troubleshooting

## Configuration does not match this run

Use the configuration that created the run or create a new run. This prevents mixed datasets.

## Search or core quota is low

The client waits when configured, preserves separate quota reserves and resumes from checkpoints.
Inspect `gem run status --run-id <ID>` and the `api_calls` table.

## GraphQL fails

Hybrid enrichment falls back to optimized REST per repository. Select
`enrichment_strategy = "optimized_rest"` if GraphQL is consistently unavailable.

## No rules or clusters

Run collection, cleaning and rule mining in order. Check the cleaning funnel: strict package or
support thresholds may leave too few repositories or technologies. Clustering needs at least three
kept repositories and two technologies.

## Report is incomplete

The report deliberately shows missing stages instead of fabricating results. Run `mine-rules`,
`cluster` and `robustness`, then regenerate `report`.
