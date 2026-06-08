from localmail.config import SearchConfig


def test_searchconfig_has_rewriter_cache_defaults():
    cfg = SearchConfig()
    assert cfg.rewriter_cache_size == 128
    assert cfg.rewriter_cache_ttl_s == 1200
