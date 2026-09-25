# Evaluation: gold.jsonl (cuiflow)

- Documents: 30; scope: ANATOMY, DISORDER, DRUG, FINDING, PROCEDURE; cuiflow 0.1.0; 2026-09-25T05:12:39Z
- **Caveat:** Reference sources: adjudicated:claude-draft. Not human-annotated: scores are agreement with this reference, not accuracy.

## Concepts

| mode | ref | pred | overlap P / R / F1 | exact F1 | CUI-set F1 |
|---|---:|---:|---|---:|---:|
| `single:mmlite` | 843 | 5818 | 0.104 / 0.717 / **0.181** | 0.180 | 0.195 |
| `single:umlsmatch` | 843 | 2928 | 0.218 / 0.756 / **0.338** | 0.336 | 0.382 |
| `ensemble:union` | 843 | 7077 | 0.096 / 0.805 / **0.172** | 0.171 | 0.191 |
| `ensemble:consensus` | 843 | 1670 | 0.337 / 0.667 / **0.447** | 0.445 | 0.459 |
| `ensemble:hybrid_staged` | 843 | 5818 | 0.104 / 0.717 / **0.181** | 0.180 | 0.195 |

## Assertions (agreement with the reference's labels, on overlap-matched pairs)

| mode | attribute | ref labels | positives | coverage | P / R / F1 |
|---|---|---:|---:|---:|---|
| `single:mmlite` | conditional | 604 | 59 | 0.000 | 0.000 / 0.000 / 0.000 |
| `single:mmlite` | history_of | 604 | 51 | 0.000 | 0.000 / 0.000 / 0.000 |
| `single:mmlite` | negated | 604 | 129 | 1.000 | 0.989 / 0.705 / 0.824 |
| `single:mmlite` | subject | 604 | 43 | 0.000 | 0.000 / 0.000 / 0.000 |
| `single:mmlite` | uncertain | 604 | 26 | 0.000 | 0.000 / 0.000 / 0.000 |
| `single:umlsmatch` | conditional | 637 | 60 | 0.000 | 0.000 / 0.000 / 0.000 |
| `single:umlsmatch` | history_of | 637 | 56 | 1.000 | 0.708 / 0.304 / 0.425 |
| `single:umlsmatch` | negated | 637 | 132 | 1.000 | 0.975 / 0.871 / 0.920 |
| `single:umlsmatch` | subject | 637 | 42 | 1.000 | 0.778 / 0.500 / 0.609 |
| `single:umlsmatch` | uncertain | 637 | 27 | 1.000 | 0.864 / 0.704 / 0.775 |
| `ensemble:union` | conditional | 679 | 64 | 0.000 | 0.000 / 0.000 / 0.000 |
| `ensemble:union` | history_of | 679 | 58 | 0.938 | 0.708 / 0.293 / 0.415 |
| `ensemble:union` | negated | 679 | 134 | 1.000 | 0.975 / 0.866 / 0.917 |
| `ensemble:union` | subject | 679 | 44 | 0.938 | 0.778 / 0.477 / 0.592 |
| `ensemble:union` | uncertain | 679 | 28 | 0.938 | 0.864 / 0.679 / 0.760 |
| `ensemble:consensus` | conditional | 562 | 55 | 0.000 | 0.000 / 0.000 / 0.000 |
| `ensemble:consensus` | history_of | 562 | 49 | 1.000 | 0.650 / 0.265 / 0.377 |
| `ensemble:consensus` | negated | 562 | 127 | 1.000 | 0.974 / 0.866 / 0.917 |
| `ensemble:consensus` | subject | 562 | 41 | 1.000 | 0.778 / 0.512 / 0.618 |
| `ensemble:consensus` | uncertain | 562 | 25 | 1.000 | 0.850 / 0.680 / 0.756 |
| `ensemble:hybrid_staged` | conditional | 604 | 59 | 0.000 | 0.000 / 0.000 / 0.000 |
| `ensemble:hybrid_staged` | history_of | 604 | 51 | 1.000 | 0.650 / 0.255 / 0.366 |
| `ensemble:hybrid_staged` | negated | 604 | 129 | 1.000 | 0.974 / 0.868 / 0.918 |
| `ensemble:hybrid_staged` | subject | 604 | 43 | 1.000 | 0.778 / 0.488 / 0.600 |
| `ensemble:hybrid_staged` | uncertain | 604 | 26 | 1.000 | 0.857 / 0.692 / 0.766 |

## Engines

- `mmlite` 0.1.3: ivf
- `umlsmatch` 0.1.0: umls_sno_rx.sqlite
