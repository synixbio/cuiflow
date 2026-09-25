"""Gold-set scoring shared by every engine and mode (DESIGN_PLAN §11, Phase 1).

Every accuracy figure must be reported with the dictionary (UMLS release and sources) and the
note set that produced it.
"""

from cuiflow.evaluation.metrics import Score, score_mentions

__all__ = ["Score", "score_mentions"]
