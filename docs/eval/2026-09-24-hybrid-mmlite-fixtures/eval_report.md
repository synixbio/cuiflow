# Evaluation: fixtures (mmlite-fixtures)

- Documents: 12; scope: ANATOMY, DISORDER, DRUG, FINDING, PROCEDURE; cuiflow 0.1.0; 2026-09-25T05:12:37Z
- **Caveat:** Java MetaMapLite 3.6.2rc8 output, not a gold standard: scores are agreement with MetaMapLite, which favours mmlite (a MetaMapLite port). Concepts only: MetaMapLite's JSON carries no assertion labels.

## Concepts

| mode | ref | pred | overlap P / R / F1 | exact F1 | CUI-set F1 |
|---|---:|---:|---|---:|---:|
| `single:mmlite` | 495 | 490 | 0.984 / 0.974 / **0.979** | 0.979 | 0.975 |
| `single:umlsmatch` | 495 | 387 | 0.473 / 0.370 / **0.415** | 0.408 | 0.432 |
| `ensemble:hybrid_staged` | 495 | 490 | 0.984 / 0.974 / **0.979** | 0.979 | 0.975 |

## Engines

- `mmlite` 0.1.3: ivf
- `umlsmatch` 0.1.0: umls_sno_rx.sqlite
