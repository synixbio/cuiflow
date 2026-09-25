# Evaluation: gold.jsonl (cuiflow)

- Documents: 30; scope: ANATOMY, DISORDER, DRUG, FINDING, PROCEDURE; cuiflow 0.1.0; 2026-09-25T05:12:55Z
- **Caveat:** Reference sources: adjudicated:claude-draft. Not human-annotated: scores are agreement with this reference, not accuracy.

## Concepts

| mode | ref | pred | overlap P / R / F1 | exact F1 | CUI-set F1 |
|---|---:|---:|---|---:|---:|
| `single:mmlite` | 843 | 1668 | 0.340 / 0.673 / **0.452** | 0.448 | 0.456 |
| `single:umlsmatch` | 843 | 1364 | 0.410 / 0.663 / **0.507** | 0.502 | 0.523 |
| `ensemble:union` | 843 | 1716 | 0.335 / 0.681 / **0.449** | 0.446 | 0.453 |
| `ensemble:consensus` | 843 | 1141 | 0.464 / 0.627 / **0.533** | 0.530 | 0.546 |
| `ensemble:hybrid_staged` | 843 | 1668 | 0.340 / 0.673 / **0.452** | 0.448 | 0.456 |

## Assertions (agreement with the reference's labels, on overlap-matched pairs)

| mode | attribute | ref labels | positives | coverage | P / R / F1 |
|---|---|---:|---:|---:|---|
| `single:mmlite` | conditional | 567 | 58 | 0.000 | 0.000 / 0.000 / 0.000 |
| `single:mmlite` | history_of | 567 | 47 | 0.000 | 0.000 / 0.000 / 0.000 |
| `single:mmlite` | negated | 567 | 118 | 1.000 | 0.988 / 0.720 / 0.833 |
| `single:mmlite` | subject | 567 | 40 | 0.000 | 0.000 / 0.000 / 0.000 |
| `single:mmlite` | uncertain | 567 | 23 | 0.000 | 0.000 / 0.000 / 0.000 |
| `single:umlsmatch` | conditional | 559 | 58 | 0.000 | 0.000 / 0.000 / 0.000 |
| `single:umlsmatch` | history_of | 559 | 48 | 1.000 | 0.611 / 0.229 / 0.333 |
| `single:umlsmatch` | negated | 559 | 117 | 1.000 | 0.971 / 0.863 / 0.914 |
| `single:umlsmatch` | subject | 559 | 40 | 1.000 | 0.833 / 0.500 / 0.625 |
| `single:umlsmatch` | uncertain | 559 | 22 | 1.000 | 0.875 / 0.636 / 0.737 |
| `ensemble:union` | conditional | 574 | 60 | 0.000 | 0.000 / 0.000 / 0.000 |
| `ensemble:union` | history_of | 574 | 47 | 0.927 | 0.562 / 0.192 / 0.286 |
| `ensemble:union` | negated | 574 | 115 | 1.000 | 0.970 / 0.852 / 0.907 |
| `ensemble:union` | subject | 574 | 40 | 0.927 | 0.833 / 0.500 / 0.625 |
| `ensemble:union` | uncertain | 574 | 23 | 0.927 | 0.875 / 0.609 / 0.718 |
| `ensemble:consensus` | conditional | 529 | 54 | 0.000 | 0.000 / 0.000 / 0.000 |
| `ensemble:consensus` | history_of | 529 | 46 | 1.000 | 0.611 / 0.239 / 0.344 |
| `ensemble:consensus` | negated | 529 | 117 | 1.000 | 0.971 / 0.863 / 0.914 |
| `ensemble:consensus` | subject | 529 | 39 | 1.000 | 0.833 / 0.513 / 0.635 |
| `ensemble:consensus` | uncertain | 529 | 22 | 1.000 | 0.875 / 0.636 / 0.737 |
| `ensemble:hybrid_staged` | conditional | 567 | 58 | 0.000 | 0.000 / 0.000 / 0.000 |
| `ensemble:hybrid_staged` | history_of | 567 | 47 | 1.000 | 0.588 / 0.213 / 0.312 |
| `ensemble:hybrid_staged` | negated | 567 | 118 | 1.000 | 0.971 / 0.864 / 0.915 |
| `ensemble:hybrid_staged` | subject | 567 | 40 | 1.000 | 0.833 / 0.500 / 0.625 |
| `ensemble:hybrid_staged` | uncertain | 567 | 23 | 1.000 | 0.882 / 0.652 / 0.750 |

## Engines

- `mmlite` 0.1.3: ivf
- `umlsmatch` 0.1.0: umls_sno_rx.sqlite
