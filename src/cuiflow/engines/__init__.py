"""Engine adapters and the factory that builds them from a configuration.

Adapters import their engine lazily, so a mode loads only what it needs: a ``single:mmlite``
worker never pays for umlsmatch's dictionary or spaCy model (DESIGN_PLAN §7.2).
"""

from __future__ import annotations

from cuiflow.core.config import ExtractionConfig
from cuiflow.core.enums import EngineMode, OverlapPolicy
from cuiflow.engines.base import Engine, EngineInfo

__all__ = ["Engine", "EngineInfo", "create_base_engine", "create_engine"]


def create_base_engine(name: str, config: ExtractionConfig) -> Engine:
    """Build one of the two underlying engines."""
    if name == "mmlite":
        from cuiflow.engines.mmlite import MmliteEngine

        return MmliteEngine(config.mmlite_index, negation=config.mmlite_negation)
    if name == "umlsmatch":
        from cuiflow.engines.umlsmatch import UmlsmatchEngine

        return UmlsmatchEngine(
            config.umlsmatch_db,
            profile=config.umlsmatch_profile,
            resolve_overlaps=config.overlaps is OverlapPolicy.LONGEST,
        )
    raise ValueError(f"unknown engine {name!r}")


def create_engine(config: ExtractionConfig) -> Engine:
    """The engine (single or ensemble) that ``config.mode`` asks for."""
    mode = config.mode
    if not mode.is_ensemble:
        return create_base_engine(mode.engines[0], config)
    from cuiflow.engines.ensemble import EnsembleEngine, HybridEngine

    built: dict[str, Engine] = {}
    try:
        for name in mode.engines:
            built[name] = create_base_engine(name, config)
    except BaseException:
        for e in built.values():
            e.close()
        raise
    if mode is EngineMode.HYBRID:
        try:
            return HybridEngine(built["mmlite"], built["umlsmatch"])  # type: ignore[arg-type]
        except BaseException:
            for e in built.values():
                e.close()
            raise
    return EnsembleEngine(built, mode, config.conflict_policy)
