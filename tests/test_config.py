from __future__ import annotations

import textwrap
from collections.abc import Callable
from pathlib import Path

import pytest

from cuiflow.core.config import ExtractionConfig, load_config
from cuiflow.core.enums import EngineMode, OverlapPolicy


@pytest.fixture(autouse=True)
def _no_local_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # load_config reads ./cuiflow.toml when it exists; a developer's own copy must not leak in.
    monkeypatch.chdir(tmp_path)


def test_defaults() -> None:
    cfg = load_config(environ={}, config_file=None)
    assert cfg.mode is EngineMode.UMLSMATCH
    assert cfg.profile == "strict"
    assert cfg.include_text is False


def test_precedence_explicit_over_env_over_file(tmp_path: Path) -> None:
    f = tmp_path / "cuiflow.toml"
    f.write_text('[cuiflow]\nmode = "single:mmlite"\ncode_systems = ["RXNORM"]\n', encoding="utf-8")
    env = {"CUIFLOW_MODE": "ensemble:union", "CUIFLOW_INCLUDE_TEXT": "true"}

    cfg = load_config(environ=env, config_file=f)
    assert cfg.mode is EngineMode.UNION  # env beats file
    assert cfg.code_systems == ("RXNORM",)  # file value survives where nothing overrides it
    assert cfg.include_text is True

    cfg = load_config({"mode": "single:umlsmatch", "profile": None}, environ=env, config_file=f)
    assert cfg.mode is EngineMode.UMLSMATCH  # explicit beats env; None means "not given"


def test_profile_supplies_defaults_only() -> None:
    cfg = load_config({"profile": "high_precision"}, environ={})
    assert cfg.mode is EngineMode.CONSENSUS
    assert cfg.overlaps is OverlapPolicy.LONGEST
    cfg = load_config({"profile": "high_precision", "overlaps": "keep"}, environ={})
    assert cfg.overlaps is OverlapPolicy.KEEP


def test_umlsmatch_profile_mapping() -> None:
    assert ExtractionConfig(profile="clinical_recall").umlsmatch_profile == "clinical_recall"
    assert ExtractionConfig.from_profile("high_precision").umlsmatch_profile == "strict"


def test_a_profile_that_would_change_nothing_is_an_error(tmp_path: Path) -> None:
    cfg = ExtractionConfig.from_profile("high_precision", overlaps="keep")
    assert (cfg.mode, cfg.overlaps) == (EngineMode.CONSENSUS, OverlapPolicy.KEEP)
    with pytest.raises(ValueError, match="would change nothing"):
        ExtractionConfig(profile="high_precision")  # named, never applied
    # A config file that pins both settings would silently outrank the profile.
    pinned = tmp_path / "pinned.toml"
    pinned.write_text('[cuiflow]\nmode = "single:umlsmatch"\noverlaps = "keep"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="remove mode and overlaps"):
        load_config({"profile": "high_precision"}, config_file=pinned, environ={})
    # The example file leaves them to the profile.
    example = Path(__file__).resolve().parents[1] / "cuiflow.example.toml"
    cfg = load_config({"profile": "high_precision"}, config_file=example, environ={})
    assert (cfg.mode, cfg.overlaps) == (EngineMode.CONSENSUS, OverlapPolicy.LONGEST)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"profile": "fast"}, "unknown profile"),
        ({"mode": "single:ctakes"}, "invalid mode"),
        ({"code_systems": "SNOMED"}, "unknown code system"),
        ({"mmlite_negation": "negspacy"}, "mmlite_negation"),
    ],
)
def test_invalid_values_rejected(overrides: dict[str, str], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        load_config(overrides, environ={})


def test_unknown_file_key_rejected(tmp_path: Path) -> None:
    f = tmp_path / "c.toml"
    f.write_text('modee = "x"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="unknown setting"):
        load_config(environ={}, config_file=f)


def test_to_dict_is_json_ready() -> None:
    d = ExtractionConfig(terminology_db=Path("t.sqlite"), code_systems=("LNC",)).to_dict()
    assert d["terminology_db"] == "t.sqlite" and d["code_systems"] == ["LNC"]
    assert d["mode"] == "single:umlsmatch"


def test_constructor_coerces_like_load_config() -> None:
    cfg = ExtractionConfig(
        mode="single:mmlite",  # type: ignore[arg-type]
        code_systems=["RXNORM"],  # type: ignore[arg-type]
        include_text="yes",  # type: ignore[arg-type]
        ancestor_depth="2",  # type: ignore[arg-type]
        terminology_db="data/t.sqlite",  # type: ignore[arg-type]
    )
    assert cfg.mode is EngineMode.MMLITE and cfg.mode.is_ensemble is False
    assert cfg.code_systems == ("RXNORM",)
    assert cfg.include_text is True and cfg.ancestor_depth == 2
    assert cfg.terminology_db == Path("data/t.sqlite")
    hash(cfg)  # frozen and hashable, not holding a list
    assert cfg == load_config(
        {
            "mode": "single:mmlite",
            "code_systems": "RXNORM",
            "include_text": "true",
            "ancestor_depth": 2,
            "terminology_db": "data/t.sqlite",
        },
        environ={},
    )


def test_package_docstring_example_runs(patch_extractor: Callable[..., None]) -> None:
    import cuiflow

    patch_extractor()
    code = cuiflow.__doc__.split("\n\n")[2] if cuiflow.__doc__ else ""
    assert "extract" in code
    exec(textwrap.dedent(code), {})  # the example in `help(cuiflow)` must work


@pytest.mark.parametrize("value", ["ture", "Y", "2", "enabled"])
def test_boolean_typos_are_errors(value: str) -> None:
    with pytest.raises(ValueError, match="include_text must be true or false"):
        load_config(environ={"CUIFLOW_INCLUDE_TEXT": value})
    assert load_config(environ={"CUIFLOW_INCLUDE_TEXT": "Off"}).include_text is False


@pytest.mark.parametrize("value", [True, 1.5])
def test_non_integers_are_not_depths(value: object) -> None:
    with pytest.raises(ValueError, match="ancestor_depth must be an integer"):
        ExtractionConfig(ancestor_depth=value, code_systems=("ICD10CM",))  # type: ignore[arg-type]


def test_paths_in_a_config_file_are_relative_to_it(tmp_path: Path) -> None:
    site = tmp_path / "site"
    site.mkdir()
    f = site / "cuiflow.toml"
    f.write_text(
        f'mmlite_index = "data/ivf"\numlsmatch_db = "{(tmp_path / "u.sqlite").as_posix()}"\n',
        encoding="utf-8",
    )
    cfg = load_config(environ={"CUIFLOW_TERMINOLOGY_DB": "t.sqlite"}, config_file=f)
    assert cfg.mmlite_index == site / "data" / "ivf"  # not ./data/ivf
    assert cfg.umlsmatch_db == tmp_path / "u.sqlite"  # absolute paths are kept
    assert cfg.terminology_db == Path("t.sqlite")  # the environment's stay CWD-relative
    # ./cuiflow.toml resolves to the same relative paths as before.
    (tmp_path / "cuiflow.toml").write_text('mmlite_index = "data/ivf"\n', encoding="utf-8")
    assert load_config(environ={}).mmlite_index == Path("data/ivf")
