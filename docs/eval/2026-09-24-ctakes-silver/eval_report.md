# Evaluation: silver.jsonl (ctakes-silver)

- Documents: 20; scope: ANATOMY, DISORDER, DRUG, FINDING, PROCEDURE; cuiflow 0.1.0; 2026-09-25T05:12:15Z
- **Caveat:** Java cTAKES output, not a gold standard: scores are agreement with cTAKES, which favours umlsmatch (a cTAKES reimplementation). cTAKES used its shipped 2016AB dictionary; the engines here may use another release. cTAKES' assertion labels are measurably wrong on some constructions (see umlsmatch docs/ADJUDICATION_RESULTS.md).

## Concepts

| mode | ref | pred | overlap P / R / F1 | exact F1 | CUI-set F1 |
|---|---:|---:|---|---:|---:|
| `single:mmlite` | 1766 | 4617 | 0.204 / 0.533 / **0.295** | 0.285 | 0.326 |
| `single:umlsmatch` | 1766 | 2174 | 0.671 / 0.826 / **0.741** | 0.738 | 0.757 |
| `ensemble:union` | 1766 | 5505 | 0.278 / 0.865 / **0.420** | 0.419 | 0.430 |
| `ensemble:consensus` | 1766 | 1287 | 0.678 / 0.494 / **0.572** | 0.551 | 0.615 |

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
| `ensemble:union` | conditional | 1528 | 3 | 0.000 | 0.000 / 0.000 / 0.000 |
| `ensemble:union` | history_of | 1528 | 17 | 0.893 | 0.333 / 0.471 / 0.390 |
| `ensemble:union` | negated | 1528 | 100 | 1.000 | 0.513 / 0.960 / 0.669 |
| `ensemble:union` | subject | 1528 | 1 | 0.954 | 0.125 / 1.000 / 0.222 |
| `ensemble:union` | uncertain | 1528 | 15 | 0.954 | 0.000 / 0.000 / 0.000 |
| `ensemble:consensus` | conditional | 873 | 3 | 0.000 | 0.000 / 0.000 / 0.000 |
| `ensemble:consensus` | history_of | 873 | 6 | 0.936 | 0.500 / 1.000 / 0.667 |
| `ensemble:consensus` | negated | 873 | 76 | 1.000 | 0.630 / 0.987 / 0.769 |
| `ensemble:consensus` | subject | 873 | 0 | 1.000 | 0.000 / 0.000 / 0.000 |
| `ensemble:consensus` | uncertain | 873 | 8 | 1.000 | 0.000 / 0.000 / 0.000 |

## Engines

- `mmlite` 0.1.3: ivf
- `umlsmatch` 0.1.0: umls_sno_rx.sqlite
