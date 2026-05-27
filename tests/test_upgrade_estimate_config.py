"""Tests for the UpgradeEstimateConfig pydantic sub-model."""

from localmail.config import Config, UpgradeEstimateConfig


def test_upgrade_estimate_config_defaults():
    cfg = UpgradeEstimateConfig()
    assert cfg.fts_v2_blowup_factor == 1.5
    assert cfg.gin_size_factor == 0.4
    assert cfg.table_rewrite_mb_per_sec == 80.0
    assert cfg.gin_build_mb_per_sec == 30.0


def test_upgrade_estimate_config_overrides():
    cfg = UpgradeEstimateConfig(
        fts_v2_blowup_factor=2.0,
        gin_size_factor=0.5,
        table_rewrite_mb_per_sec=40.0,
        gin_build_mb_per_sec=15.0,
    )
    assert cfg.fts_v2_blowup_factor == 2.0
    assert cfg.gin_size_factor == 0.5
    assert cfg.table_rewrite_mb_per_sec == 40.0
    assert cfg.gin_build_mb_per_sec == 15.0


def test_config_has_upgrade_subsection_by_default():
    cfg = Config(database={"dsn": "postgresql://x"})
    assert isinstance(cfg.upgrade, UpgradeEstimateConfig)
    assert cfg.upgrade.table_rewrite_mb_per_sec == 80.0


def test_config_round_trip_with_upgrade_section():
    """Parsing a TOML-like dict with an [upgrade] block must round-trip."""
    cfg = Config.model_validate({
        "database": {"dsn": "postgresql://x"},
        "upgrade": {"table_rewrite_mb_per_sec": 20.0},
    })
    assert cfg.upgrade.table_rewrite_mb_per_sec == 20.0
    # Untouched field still has the default.
    assert cfg.upgrade.fts_v2_blowup_factor == 1.5
