"""Phase 5 tests: classifier, sentiment, relevance, credibility, novelty
(sections 23-24). See docs/09_tests_plan.md."""
from src.news.classifier import CATEGORIES, classify_event
from src.news.credibility import DEFAULT_CREDIBILITY, score_credibility
from src.news.novelty import score_novelty
from src.news.relevance import keyword_matches, score_relevance
from src.news.sentiment import score_sentiment


# --- classifier ---


def test_classify_event_hack_security():
    assert classify_event({"title": "Exchange hacked, $50M drained", "summary": ""}) == "HACK_SECURITY"


def test_classify_event_regulation():
    assert classify_event({"title": "SEC files lawsuit against exchange", "summary": ""}) == "REGULATION"


def test_classify_event_etf():
    assert classify_event({"title": "BlackRock spot ETF sees record inflows", "summary": ""}) == "ETF"


def test_classify_event_macro():
    assert classify_event({"title": "Fed holds interest rates steady", "summary": ""}) == "MACRO"


def test_classify_event_exchange():
    assert classify_event({"title": "Binance lists new trading pair", "summary": ""}) == "EXCHANGE"


def test_classify_event_partnership_adoption():
    assert classify_event({"title": "Company announces partnership to launch new product", "summary": ""}) == "PARTNERSHIP_ADOPTION"


def test_classify_event_falls_back_to_other():
    result = classify_event({"title": "A completely unrelated headline about gardening", "summary": ""})
    assert result == "OTHER"
    assert result in CATEGORIES


def test_classify_event_priority_hack_beats_exchange():
    # Mentions "exchange" and "hack" -> more specific HACK_SECURITY wins.
    assert classify_event({"title": "Exchange hacked in major exploit", "summary": ""}) == "HACK_SECURITY"


# --- regression: 2026-08-18 live validation caught naive substring
# matching misfiring on real SEC/Fed feed content (see relevance.py's
# docstring and docs/10_news_radar.md) ---


def test_classify_event_issued_does_not_trigger_regulation_via_sued():
    # "issued" contains "sued" as a raw substring — must not classify as
    # REGULATION just because of that. "fed" (exact match) is the only
    # real trigger here, so the expected category is MACRO.
    result = classify_event({"title": "Fed issued a routine statement today", "summary": ""})
    assert result == "MACRO"


def test_classify_event_securities_boilerplate_alone_is_other():
    # The SEC's own institutional name must not, by itself, trigger
    # REGULATION via a "sec"-inside-"Securities" substring match.
    result = classify_event(
        {"title": "Gotbit Consulting LLC and Fedor Kedrov", "summary": "U.S. Securities and Exchange Commission"}
    )
    assert result == "OTHER"


def test_classify_event_hacked_and_hacking_still_match_via_suffix():
    # Regression guard: the word-boundary fix must not break inflected
    # forms of longer keywords like "hack".
    assert classify_event({"title": "Exchange was hacked overnight", "summary": ""}) == "HACK_SECURITY"
    assert classify_event({"title": "Group known for hacking DeFi protocols", "summary": ""}) == "HACK_SECURITY"


# --- sentiment ---


def test_score_sentiment_bullish_words_positive():
    score = score_sentiment({"title": "Bitcoin rallies as ETF approval sparks inflow", "summary": ""})
    assert score > 0


def test_score_sentiment_bearish_words_negative():
    score = score_sentiment({"title": "Exchange hacked, faces lawsuit amid collapse", "summary": ""})
    assert score < 0


def test_score_sentiment_neutral_when_no_lexicon_hits():
    assert score_sentiment({"title": "Quarterly report published", "summary": "Routine filing"}) == 0.0


def test_score_sentiment_bounded_in_range():
    score = score_sentiment({"title": "surge rally soar bullish approval adoption", "summary": ""})
    assert -1.0 <= score <= 1.0


# --- relevance ---


def test_score_relevance_no_keywords_matched_is_zero():
    result = score_relevance({"title": "Local bakery wins award", "summary": ""}, keywords=["Bitcoin", "SEC"])
    assert result["market_relevance"] == 0.0
    assert result["affected_assets"] == []


def test_score_relevance_matches_keywords_and_extracts_assets():
    result = score_relevance(
        {"title": "Bitcoin ETF approved by SEC", "summary": ""}, keywords=["Bitcoin", "ETF", "SEC"]
    )
    assert result["market_relevance"] > 0
    assert "BTC" in result["affected_assets"]
    assert set(result["matched_keywords"]) == {"Bitcoin", "ETF", "SEC"}


def test_score_relevance_caps_at_one():
    keywords = ["Bitcoin", "ETF", "SEC", "Fed", "regulation", "stablecoin"]
    result = score_relevance(
        {"title": "Bitcoin ETF SEC Fed regulation stablecoin news roundup", "summary": ""}, keywords=keywords
    )
    assert result["market_relevance"] == 1.0


def test_score_relevance_extracts_multiple_assets():
    result = score_relevance({"title": "Ethereum and Solana rally alongside Bitcoin", "summary": ""}, keywords=["rally"])
    assert set(result["affected_assets"]) == {"BTC", "ETH", "SOL"}


# --- regression: 2026-08-18 live validation (real user run against the
# live feeds) caught these exact false positives; see relevance.py's
# docstring and docs/10_news_radar.md ---


def test_keyword_matches_rejects_substring_inside_unrelated_word():
    assert keyword_matches("together we stand", "eth") is False
    assert keyword_matches("please resolve this issue", "sol") is False
    assert keyword_matches("Securities and Exchange Commission", "sec") is False
    assert keyword_matches("the company issued a statement", "sued") is False


def test_keyword_matches_still_matches_standalone_ticker():
    assert keyword_matches("ETH price surged today", "eth") is True
    assert keyword_matches("SOL is up 5%", "sol") is True
    assert keyword_matches("the SEC announced", "sec") is True


def test_keyword_matches_handles_etf_plural():
    assert keyword_matches("Bitcoin ETFs saw record inflows", "etf") is True


def test_keyword_matches_preserves_suffix_matching_for_longer_keywords():
    assert keyword_matches("the exchange was hacked", "hack") is True
    assert keyword_matches("new regulations proposed", "regulat") is True


def test_score_relevance_real_case_sec_litigation_against_unrelated_defendant():
    """The exact false positive from the 2026-08-18 live run: an SEC
    litigation release naming unrelated defendants, with the SEC's own
    boilerplate name in the summary, was incorrectly scored as relevant
    and even tagged with an affected asset."""
    event = {
        "title": 'Gotbit Consulting LLC a/k/a Gotbit Hedge Fund and Fedor Kedrov',
        "summary": "U.S. Securities and Exchange Commission litigation release.",
    }
    keywords = [
        "Bitcoin", "BTC", "Ethereum", "ETH", "Solana", "SOL", "ETF", "SEC", "Fed",
        "FOMC", "CPI", "inflation", "interest rates", "liquidation", "hack",
        "exploit", "exchange", "Kraken", "Coinbase", "Binance", "BlackRock",
        "regulation", "stablecoin", "USDC",
    ]
    result = score_relevance(event, keywords=keywords)
    assert result["market_relevance"] == 0.0
    assert result["affected_assets"] == []


def test_score_relevance_genuine_sec_crypto_case_still_matches():
    # Sanity check: the boilerplate strip must not silently blind the
    # filter to real SEC crypto enforcement actions.
    event = {
        "title": "SEC charges crypto exchange with unregistered securities offering",
        "summary": "The Securities and Exchange Commission announced charges today.",
    }
    result = score_relevance(event, keywords=["SEC", "exchange", "Bitcoin"])
    assert result["market_relevance"] > 0
    assert "exchange" in result["matched_keywords"] or "SEC" in result["matched_keywords"]


# --- credibility ---


def test_score_credibility_known_source():
    feeds = [{"name": "CoinDesk", "credibility": 0.7}]
    assert score_credibility("CoinDesk", feeds_config=feeds) == 0.7


def test_score_credibility_case_insensitive_lookup():
    feeds = [{"name": "CoinDesk", "credibility": 0.7}]
    assert score_credibility("coindesk", feeds_config=feeds) == 0.7


def test_score_credibility_unknown_source_returns_default():
    assert score_credibility("Some Random Blog", feeds_config=[{"name": "CoinDesk", "credibility": 0.7}]) == DEFAULT_CREDIBILITY


def test_score_credibility_no_config_returns_default():
    assert score_credibility("CoinDesk", feeds_config=None) == DEFAULT_CREDIBILITY


# --- novelty ---


def test_score_novelty_empty_history_is_fully_novel():
    assert score_novelty({"title": "Bitcoin hits new high"}, []) == 1.0


def test_score_novelty_near_duplicate_scores_low():
    history = [{"title": "Bitcoin surges past $100k on ETF inflows"}]
    score = score_novelty({"title": "Bitcoin surges past $100k amid ETF inflows"}, history)
    assert score < 0.3


def test_score_novelty_unrelated_history_stays_high():
    history = [{"title": "Local bakery wins award"}]
    score = score_novelty({"title": "Bitcoin hits new all-time high"}, history)
    assert score > 0.7


def test_score_novelty_accepts_plain_string_history():
    score = score_novelty({"title": "Bitcoin hits new high"}, ["Bitcoin hits new high"])
    assert score == 0.0
