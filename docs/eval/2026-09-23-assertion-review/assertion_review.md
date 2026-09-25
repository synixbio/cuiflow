# Assertion review: 30 notes, up to 100 per cell, seed 0

Precision of positives (judged yes / judged) and negative predictive value of negatives (judged no / judged), Wilson 95% intervals; `unclear` is left out of both. Recall is estimated as PPV·P / (PPV·P + (1−NPV)·N) from the mode's P positives and N negatives.

| mode | attribute | positives P | precision | negatives N | NPV | unclear | recall (est.) |
|---|---|---:|---|---:|---|---:|---:|
| `single:mmlite` | negated | 134 | 84/93 = 0.90 (0.83–0.95) | 2679 | 97/99 = 0.98 (0.93–0.99) | 8 | 0.69 |
| `single:umlsmatch` | history_of | 80 | 24/70 = 0.34 (0.24–0.46) | 2283 | 90/91 = 0.99 (0.94–1.00) | 19 | 0.52 |
| `single:umlsmatch` | negated | 277 | 60/74 = 0.81 (0.71–0.88) | 2086 | 98/99 = 0.99 (0.94–1.00) | 27 | 0.91 |
| `single:umlsmatch` | uncertain | 58 | 31/40 = 0.78 (0.62–0.88) | 2305 | 100/100 = 1.00 (0.96–1.00) | 18 | 1.00 |
