# Evaluation: silver.jsonl (ctakes-silver)

- Documents: 20; scope: ANATOMY, DISORDER, DRUG, FINDING, PROCEDURE; cuiflow 0.1.0; 2026-09-25T05:12:29Z
- **Caveat:** Java cTAKES output, not a gold standard: scores are agreement with cTAKES, which favours umlsmatch (a cTAKES reimplementation). cTAKES used its shipped 2016AB dictionary; the engines here may use another release. cTAKES' assertion labels are measurably wrong on some constructions (see umlsmatch docs/ADJUDICATION_RESULTS.md).

## Concepts

| mode | ref | pred | overlap P / R / F1 | exact F1 | CUI-set F1 |
|---|---:|---:|---|---:|---:|
| `single:mmlite` | 1766 | 4617 | 0.204 / 0.533 / **0.295** | 0.285 | 0.326 |
| `single:umlsmatch` | 1766 | 2174 | 0.671 / 0.826 / **0.741** | 0.738 | 0.757 |
| `ensemble:hybrid_staged` | 1766 | 4617 | 0.204 / 0.533 / **0.295** | 0.285 | 0.326 |

## Assertions (agreement with the reference's labels, on overlap-matched pairs)

| mode | attribute | ref labels | positives | coverage | P / R / F1 |
|---|---|---:|---:|---:|---|
| `single:mmlite` | conditional | 942 | 3 | 0.000 | 0.000 / 0.000 / 0.000 |
| `single:mmlite` | history_of | 942 | 6 | 0.000 | 0.000 / 0.000 / 0.000 |
| `single:mmlite` | negated | 942 | 79 | 1.000 | 0.829 / 0.861 / 0.845 |
| `single:mmlite` | subject | 942 | 0 | 0.000 | 0.000 / 0.000 / 0.000 |
| `single:mmlite` | uncertain | 942 | 8 | 0.000 | 0.000 / 0.000 / 0.000 |
| `single:umlsmatch` | conditional | 1459 | 3 | 0.000 | 0.000 / 0.000 / 0.000 |
| `single:umlsmatch` | history_of | 1459 | 17 | 0.936 | 0.333 / 0.471 / 0.390 |
| `single:umlsmatch` | negated | 1459 | 97 | 1.000 | 0.519 / 0.990 / 0.681 |
| `single:umlsmatch` | subject | 1459 | 1 | 1.000 | 0.125 / 1.000 / 0.222 |
| `single:umlsmatch` | uncertain | 1459 | 15 | 1.000 | 0.000 / 0.000 / 0.000 |
| `ensemble:hybrid_staged` | conditional | 942 | 3 | 0.000 | 0.000 / 0.000 / 0.000 |
| `ensemble:hybrid_staged` | history_of | 942 | 6 | 0.938 | 0.500 / 1.000 / 0.667 |
| `ensemble:hybrid_staged` | negated | 942 | 79 | 1.000 | 0.623 / 0.962 / 0.756 |
| `ensemble:hybrid_staged` | subject | 942 | 0 | 1.000 | 0.000 / 0.000 / 0.000 |
| `ensemble:hybrid_staged` | uncertain | 942 | 8 | 1.000 | 0.000 / 0.000 / 0.000 |

## Engines

- `mmlite` 0.1.3: ivf
- `umlsmatch` 0.1.0: umls_sno_rx.sqlite
