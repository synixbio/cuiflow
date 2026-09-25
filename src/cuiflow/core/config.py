"""Settings, named profiles and their precedence (DESIGN_PLAN §6.2).

Precedence, highest first::

    explicit arguments / CLI flags > environment (CUIFLOW_*) > config file (cuiflow.toml)
    > profile defaults > built-in defaults

A profile only supplies defaults: anything set at a higher layer wins. A profile whose every
setting has been set back to the built-in default would change nothing, and is an error rather
than a label on a run it did not shape. Build a configuration in code with
:meth:`ExtractionConfig.from_profile`. The run manifest records the *effective* configuration,
never the flags as typed.
"""

from __future__ import annotations

import dataclasses
import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cuiflow.core.enums import CODE_SYSTEMS, ConflictPolicy, EngineMode, OverlapPolicy

ENV_PREFIX = "CUIFLOW_"
DEFAULT_PROFILE = "strict"
DEFAULT_CONFIG_FILE = Path("cuiflow.toml")

#: Profile name -> defaults for ExtractionConfig fields. ``strict`` and ``clinical_recall`` are
#: umlsmatch's own profiles, passed straight through; the others are cuiflow's.
PROFILES: dict[str, dict[str, Any]] = {
    "strict": {},
    "clinical_recall": {},
    # Proposed (DESIGN_PLAN §6.2). Consensus is measured; this profile, with ``longest``, is not.
    "high_precision": {"mode": EngineMode.CONSENSUS, "overlaps": OverlapPolicy.LONGEST},
}

#: The umlsmatch profile each cuiflow profile runs umlsmatch with.
UMLSMATCH_PROFILE: dict[str, str] = {
    "strict": "strict",
    "clinical_recall": "clinical_recall",
    "high_precision": "strict",
}


@dataclass(frozen=True, slots=True)
class ExtractionConfig:
    """Everything that decides what an extraction run produces.

    Fields accept what a file or the environment gives, and are converted to their types:
    ``ExtractionConfig(mode="single:mmlite", code_systems=["RXNORM"])`` is valid."""

    mode: EngineMode = EngineMode.UMLSMATCH
    #: Selects umlsmatch's profile and cuiflow defaults; mmlite's settings are fixed.
    profile: str = DEFAULT_PROFILE
    overlaps: OverlapPolicy = OverlapPolicy.KEEP
    conflict_policy: ConflictPolicy = ConflictPolicy.PREFER_UMLSMATCH
    #: Semantic groups to keep (cTAKES names, e.g. "DISORDER"); empty keeps all.
    semantic_groups: tuple[str, ...] = ()
    #: Vocabularies to attach codes from; empty attaches none. Needs ``terminology_db``.
    code_systems: tuple[str, ...] = ()
    #: Attach each ICD-10-CM / SNOMED CT code's ancestors up to this distance; 0 attaches none.
    #: Needs a terminology database built with MRREL.
    ancestor_depth: int = 0
    #: Attach each RxNorm code's ingredient RxCUIs. Needs a terminology database built with MRREL.
    include_ingredients: bool = False
    #: Include the note text in each DocumentResult. Off by default: it is PHI.
    include_text: bool = False
    #: Post-filter applied to engine output: "none", or "guidelines" to apply the annotation
    #: guidelines' rules (headings, template words, one concept per span, longest span).
    mention_filter: str = "none"
    #: "negex" or "context". ConText adds temporality and experiencer (mapped to subject).
    mmlite_negation: str = "negex"
    mmlite_index: Path | None = None
    umlsmatch_db: Path | None = None
    terminology_db: Path | None = None
    #: Anything else, passed through untouched (e.g. engine-specific experiments).
    extra: Mapping[str, Any] = field(default_factory=dict, hash=False)

    def __post_init__(self) -> None:
        for name in _FIELDS:
            value = getattr(self, name)
            coerced = _coerce(name, value)
            if coerced is not value:
                object.__setattr__(self, name, coerced)  # frozen: only during construction
        if self.profile not in PROFILES:
            raise ValueError(
                f"unknown profile {self.profile!r}; choose one of {', '.join(sorted(PROFILES))}"
            )
        preset = PROFILES[self.profile]
        if preset and all(getattr(self, k) == _DEFAULTS[k] != v for k, v in preset.items()):
            # Every setting the profile exists for is at its default: the profile was named but
            # not applied (a direct constructor call, or a config file that sets these keys).
            keys = " and ".join(preset)
            raise ValueError(
                f"profile {self.profile!r} sets {keys}, but this configuration has the defaults "
                f"for all of them, so the profile would change nothing. Build it with "
                f"ExtractionConfig.from_profile() or load_config(), or remove {keys} from your "
                f"config file."
            )
        unknown = set(self.code_systems) - set(CODE_SYSTEMS)
        if unknown:
            raise ValueError(
                f"unknown code system(s) {sorted(unknown)}; choose from {', '.join(CODE_SYSTEMS)}"
            )
        if not isinstance(self.ancestor_depth, int) or self.ancestor_depth < 0:
            raise ValueError(f"ancestor_depth must be an integer >= 0, got {self.ancestor_depth!r}")
        if (self.ancestor_depth or self.include_ingredients) and not self.code_systems:
            raise ValueError("ancestor_depth and include_ingredients need code_systems")
        if self.mention_filter not in ("none", "guidelines"):
            raise ValueError(
                f"mention_filter must be 'none' or 'guidelines', got {self.mention_filter!r}"
            )
        if self.mmlite_negation not in ("negex", "context"):
            raise ValueError(
                f"mmlite_negation must be 'negex' or 'context', got {self.mmlite_negation!r}"
            )

    @classmethod
    def from_profile(cls, profile: str, **fields: Any) -> ExtractionConfig:
        """A configuration with ``profile``'s defaults, then ``fields`` over them."""
        if profile not in PROFILES:
            raise ValueError(
                f"unknown profile {profile!r}; choose one of {', '.join(sorted(PROFILES))}"
            )
        return cls(**{**PROFILES[profile], **fields, "profile": profile})

    @property
    def umlsmatch_profile(self) -> str:
        return UMLSMATCH_PROFILE[self.profile]

    def to_dict(self) -> dict[str, Any]:
        """The effective configuration, JSON-serializable, for run manifests."""
        out: dict[str, Any] = {}
        for f in dataclasses.fields(self):
            value = getattr(self, f.name)
            if isinstance(value, Path):
                value = str(value)
            elif isinstance(value, tuple):
                value = list(value)
            elif isinstance(value, Mapping):
                value = dict(value)
            out[f.name] = value
        return out


_FIELDS = {f.name: f for f in dataclasses.fields(ExtractionConfig)}
_DEFAULTS = {name: f.default for name, f in _FIELDS.items()}
_PATH_FIELDS = {"mmlite_index", "umlsmatch_db", "terminology_db"}
_TUPLE_FIELDS = {"semantic_groups", "code_systems"}
_BOOL_FIELDS = {"include_text", "include_ingredients"}
_INT_FIELDS = {"ancestor_depth"}
_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})
_ENUM_FIELDS: dict[str, Any] = {
    "mode": EngineMode,
    "overlaps": OverlapPolicy,
    "conflict_policy": ConflictPolicy,
}


def _coerce(name: str, value: Any) -> Any:
    """Turn a value from a file, the environment or a caller into the field's type."""
    if value is None:
        return None
    if name in _ENUM_FIELDS:
        try:
            return _ENUM_FIELDS[name](value)
        except ValueError:
            choices = ", ".join(m.value for m in _ENUM_FIELDS[name])
            raise ValueError(f"invalid {name} {value!r}; choose one of {choices}") from None
    if name in _PATH_FIELDS:
        return value if isinstance(value, Path) else Path(value)
    if name in _TUPLE_FIELDS:
        if isinstance(value, tuple):
            return value
        if isinstance(value, str):
            return tuple(v.strip() for v in value.split(",") if v.strip())
        return tuple(value)
    if name in _BOOL_FIELDS:
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
        raise ValueError(f"{name} must be true or false, got {value!r}")
    if name in _INT_FIELDS:
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        if isinstance(value, bool) or (isinstance(value, float) and not value.is_integer()):
            raise ValueError(f"{name} must be an integer, got {value!r}")
        try:
            return int(value)
        except (TypeError, ValueError):
            raise ValueError(f"{name} must be an integer, got {value!r}") from None
    return value


def _layer(values: Mapping[str, Any], source: str) -> dict[str, Any]:
    unknown = set(values) - set(_FIELDS)
    if unknown:
        raise ValueError(f"unknown setting(s) in {source}: {', '.join(sorted(unknown))}")
    return {k: _coerce(k, v) for k, v in values.items()}


def _from_env(environ: Mapping[str, str]) -> dict[str, Any]:
    out = {}
    for name in _FIELDS:
        if name == "extra":
            continue
        raw = environ.get(ENV_PREFIX + name.upper())
        if raw is not None and raw != "":
            out[name] = raw
    return out


def _from_file(path: Path) -> dict[str, Any]:
    """A config file's layer. Relative paths in it are relative to the file, not the CWD."""
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    # Accept either top-level keys or a [cuiflow] table.
    table = data.get("cuiflow", data)
    if not isinstance(table, dict):
        raise ValueError(f"{path}: [cuiflow] must be a table")
    layer = _layer(table, str(path))
    for name in _PATH_FIELDS & layer.keys():
        if layer[name] is not None and not layer[name].is_absolute():
            layer[name] = path.parent / layer[name]
    return layer


def load_config(
    overrides: Mapping[str, Any] | None = None,
    *,
    config_file: Path | str | None = None,
    environ: Mapping[str, str] | None = None,
) -> ExtractionConfig:
    """Resolve the effective configuration from every layer.

    ``config_file`` defaults to ``./cuiflow.toml`` when that exists, so a library caller's
    ``Extractor()`` reads it too. Relative paths in a config file are relative to that file;
    elsewhere, to the working directory. Values of ``None`` in
    ``overrides`` mean "not given" (so unset CLI flags fall through to lower layers).
    """
    environ = os.environ if environ is None else environ
    file_layer: dict[str, Any] = {}
    if config_file is not None:
        file_layer = _from_file(Path(config_file))
    elif DEFAULT_CONFIG_FILE.is_file():
        file_layer = _from_file(DEFAULT_CONFIG_FILE)
    env_layer = _layer(_from_env(environ), "environment")
    explicit = _layer({k: v for k, v in (overrides or {}).items() if v is not None}, "arguments")

    merged: dict[str, Any] = {}
    for layer in (file_layer, env_layer, explicit):
        merged.update(layer)
    profile = merged.get("profile", DEFAULT_PROFILE)
    if profile not in PROFILES:
        raise ValueError(
            f"unknown profile {profile!r}; choose one of {', '.join(sorted(PROFILES))}"
        )

    effective: dict[str, Any] = dict(PROFILES[profile])
    effective.update(merged)
    return ExtractionConfig(**effective)
