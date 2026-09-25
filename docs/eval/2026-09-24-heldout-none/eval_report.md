# Evaluation: heldout.jsonl (cuiflow)

- Documents: 10; scope: ANATOMY, DISORDER, DRUG, FINDING, PROCEDURE; cuiflow 0.1.0; 2026-09-25T05:13:10Z
- **Caveat:** Reference sources: adjudicated:claude-draft. Not human-annotated: scores are agreement with this reference, not accuracy.

## Concepts

| mode | ref | pred | overlap P / R / F1 | exact F1 | CUI-set F1 |
|---|---:|---:|---|---:|---:|
| `single:mmlite` | 230 | 1201 | 0.164 / 0.857 / **0.275** | 0.275 | 0.275 |
| `single:umlsmatch` | 230 | 754 | 0.263 / 0.861 / **0.402** | 0.402 | 0.457 |
| `ensemble:union` | 230 | 1572 | 0.136 / 0.926 / **0.236** | 0.236 | 0.252 |
| `ensemble:consensus` | 230 | 383 | 0.475 / 0.791 / **0.594** | 0.594 | 0.594 |
| `ensemble:hybrid_staged` | 230 | 1201 | 0.164 / 0.857 / **0.275** | 0.275 | 0.275 |

## Assertions (agreement with the reference's labels, on overlap-matched pairs)

| mode | attribute | ref labels | positives | coverage | P / R / F1 |
|---|---|---:|---:|---:|---|
| `single:mmlite` | conditional | 197 | 42 | 0.000 | 0.000 / 0.000 / 0.000 |
| `single:mmlite` | history_of | 197 | 32 | 0.000 | 0.000 / 0.000 / 0.000 |
| `single:mmlite` | negated | 197 | 31 | 1.000 | 0.958 / 0.742 / 0.836 |
| `single:mmlite` | subject | 197 | 43 | 0.000 | 0.000 / 0.000 / 0.000 |
| `single:mmlite` | uncertain | 197 | 20 | 0.000 | 0.000 / 0.000 / 0.000 |
| `single:umlsmatch` | conditional | 198 | 42 | 0.000 | 0.000 / 0.000 / 0.000 |
| `single:umlsmatch` | history_of | 198 | 34 | 1.000 | 0.900 / 0.265 / 0.409 |
| `single:umlsmatch` | negated | 198 | 32 | 1.000 | 0.933 / 0.875 / 0.903 |
| `single:umlsmatch` | subject | 198 | 42 | 1.000 | 0.840 / 0.500 / 0.627 |
| `single:umlsmatch` | uncertain | 198 | 21 | 1.000 | 0.833 / 0.714 / 0.769 |
| `ensemble:union` | conditional | 213 | 45 | 0.000 | 0.000 / 0.000 / 0.000 |
| `ensemble:union` | history_of | 213 | 35 | 0.930 | 0.900 / 0.257 / 0.400 |
| `ensemble:union` | negated | 213 | 32 | 1.000 | 0.933 / 0.875 / 0.903 |
| `ensemble:union` | subject | 213 | 44 | 0.930 | 0.840 / 0.477 / 0.609 |
| `ensemble:union` | uncertain | 213 | 22 | 0.930 | 0.833 / 0.682 / 0.750 |
| `ensemble:consensus` | conditional | 182 | 39 | 0.000 | 0.000 / 0.000 / 0.000 |
| `ensemble:consensus` | history_of | 182 | 31 | 1.000 | 0.875 / 0.226 / 0.359 |
| `ensemble:consensus` | negated | 182 | 31 | 1.000 | 0.931 / 0.871 / 0.900 |
| `ensemble:consensus` | subject | 182 | 41 | 1.000 | 0.840 / 0.512 / 0.636 |
| `ensemble:consensus` | uncertain | 182 | 19 | 1.000 | 0.812 / 0.684 / 0.743 |
| `ensemble:hybrid_staged` | conditional | 197 | 42 | 0.000 | 0.000 / 0.000 / 0.000 |
| `ensemble:hybrid_staged` | history_of | 197 | 32 | 1.000 | 0.875 / 0.219 / 0.350 |
| `ensemble:hybrid_staged` | negated | 197 | 31 | 1.000 | 0.931 / 0.871 / 0.900 |
| `ensemble:hybrid_staged` | subject | 197 | 43 | 1.000 | 0.840 / 0.488 / 0.618 |
| `ensemble:hybrid_staged` | uncertain | 197 | 20 | 1.000 | 0.824 / 0.700 / 0.757 |

## Engines

- `mmlite` 0.1.3: ivf
- `umlsmatch` 0.1.0: umls_sno_rx.sqlite
