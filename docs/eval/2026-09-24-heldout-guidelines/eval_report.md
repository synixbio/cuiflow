# Evaluation: heldout.jsonl (cuiflow)

- Documents: 10; scope: ANATOMY, DISORDER, DRUG, FINDING, PROCEDURE; cuiflow 0.1.0; 2026-09-25T05:13:14Z
- **Caveat:** Reference sources: adjudicated:claude-draft. Not human-annotated: scores are agreement with this reference, not accuracy.

## Concepts

| mode | ref | pred | overlap P / R / F1 | exact F1 | CUI-set F1 |
|---|---:|---:|---|---:|---:|
| `single:mmlite` | 230 | 320 | 0.556 / 0.774 / **0.647** | 0.647 | 0.635 |
| `single:umlsmatch` | 230 | 291 | 0.605 / 0.765 / **0.676** | 0.676 | 0.683 |
| `ensemble:union` | 230 | 330 | 0.551 / 0.791 / **0.650** | 0.650 | 0.639 |
| `ensemble:consensus` | 230 | 244 | 0.680 / 0.722 / **0.700** | 0.700 | 0.699 |
| `ensemble:hybrid_staged` | 230 | 320 | 0.556 / 0.774 / **0.647** | 0.647 | 0.635 |

## Assertions (agreement with the reference's labels, on overlap-matched pairs)

| mode | attribute | ref labels | positives | coverage | P / R / F1 |
|---|---|---:|---:|---:|---|
| `single:mmlite` | conditional | 178 | 41 | 0.000 | 0.000 / 0.000 / 0.000 |
| `single:mmlite` | history_of | 178 | 30 | 0.000 | 0.000 / 0.000 / 0.000 |
| `single:mmlite` | negated | 178 | 26 | 1.000 | 0.950 / 0.731 / 0.826 |
| `single:mmlite` | subject | 178 | 40 | 0.000 | 0.000 / 0.000 / 0.000 |
| `single:mmlite` | uncertain | 178 | 19 | 0.000 | 0.000 / 0.000 / 0.000 |
| `single:umlsmatch` | conditional | 176 | 41 | 0.000 | 0.000 / 0.000 / 0.000 |
| `single:umlsmatch` | history_of | 176 | 30 | 1.000 | 0.857 / 0.200 / 0.324 |
| `single:umlsmatch` | negated | 176 | 26 | 1.000 | 0.917 / 0.846 / 0.880 |
| `single:umlsmatch` | subject | 176 | 40 | 1.000 | 0.870 / 0.500 / 0.635 |
| `single:umlsmatch` | uncertain | 176 | 18 | 1.000 | 0.857 / 0.667 / 0.750 |
| `ensemble:union` | conditional | 182 | 43 | 0.000 | 0.000 / 0.000 / 0.000 |
| `ensemble:union` | history_of | 182 | 29 | 0.918 | 0.833 / 0.172 / 0.286 |
| `ensemble:union` | negated | 182 | 25 | 1.000 | 0.913 / 0.840 / 0.875 |
| `ensemble:union` | subject | 182 | 40 | 0.918 | 0.870 / 0.500 / 0.635 |
| `ensemble:union` | uncertain | 182 | 19 | 0.918 | 0.857 / 0.632 / 0.727 |
| `ensemble:consensus` | conditional | 166 | 38 | 0.000 | 0.000 / 0.000 / 0.000 |
| `ensemble:consensus` | history_of | 166 | 30 | 1.000 | 0.875 / 0.233 / 0.368 |
| `ensemble:consensus` | negated | 166 | 27 | 1.000 | 0.920 / 0.852 / 0.885 |
| `ensemble:consensus` | subject | 166 | 39 | 1.000 | 0.870 / 0.513 / 0.645 |
| `ensemble:consensus` | uncertain | 166 | 18 | 1.000 | 0.857 / 0.667 / 0.750 |
| `ensemble:hybrid_staged` | conditional | 178 | 41 | 0.000 | 0.000 / 0.000 / 0.000 |
| `ensemble:hybrid_staged` | history_of | 178 | 30 | 1.000 | 0.857 / 0.200 / 0.324 |
| `ensemble:hybrid_staged` | negated | 178 | 26 | 1.000 | 0.917 / 0.846 / 0.880 |
| `ensemble:hybrid_staged` | subject | 178 | 40 | 1.000 | 0.870 / 0.500 / 0.635 |
| `ensemble:hybrid_staged` | uncertain | 178 | 19 | 1.000 | 0.867 / 0.684 / 0.765 |

## Engines

- `mmlite` 0.1.3: ivf
- `umlsmatch` 0.1.0: umls_sno_rx.sqlite
