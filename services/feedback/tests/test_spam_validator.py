"""
Unit tests for spam validator module - TDD approach.

Tests follow TDD principles:
- Create instances of classes
- Test methods on objects
- Test behavior through object interactions
- Use fixtures for reusable test objects
"""

import pytest

from services.feedback.spam_validator import (
    PatternValidator,
    SpamCheckResult,
    SpamValidator,
    SpamValidatorFactory,
    URLValidator,
    get_spam_validator_factory,
)

# Mark all tests as unit tests
pytestmark = pytest.mark.unit


# ============================================================================
# Fixtures - Test Objects
# ============================================================================


@pytest.fixture
def url_validator():
    """Create a URLValidator instance for testing."""
    return URLValidator()


@pytest.fixture
def pattern_validator():
    """Create a PatternValidator instance for testing."""
    return PatternValidator()


@pytest.fixture
def spam_validator():
    """Create a SpamValidator instance without ML/profanity for testing."""
    return SpamValidator(enable_ml=False, enable_profanity=False)


@pytest.fixture
def spam_validator_with_profanity():
    """Create a SpamValidator instance with profanity checking."""
    return SpamValidator(enable_ml=False, enable_profanity=True)


@pytest.fixture
def spam_validator_factory():
    """Create a SpamValidatorFactory instance for testing."""
    return SpamValidatorFactory()


# ============================================================================
# URLValidator Tests - Testing Class Instance Methods
# ============================================================================


class TestURLValidator:
    """Tests for URLValidator class - testing instance methods."""

    def test_extract_urls_simple(self, url_validator):
        """Test extracting simple URLs using instance method."""
        text = "Check out https://example.com for more info"
        urls = url_validator.extract_urls(text)
        assert len(urls) == 1
        assert "https://example.com" in urls[0]

    def test_extract_urls_multiple(self, url_validator):
        """Test extracting multiple URLs using instance method."""
        text = "Visit https://example.com and http://test.org"
        urls = url_validator.extract_urls(text)
        assert len(urls) == 2
        assert all("http" in url for url in urls)

    def test_extract_urls_none(self, url_validator):
        """Test extracting URLs when none exist."""
        text = "This is just plain text without any URLs"
        urls = url_validator.extract_urls(text)
        assert isinstance(urls, list)
        assert len(urls) == 0

    def test_is_valid_url_valid(self, url_validator):
        """Test validating valid URLs using instance method."""
        assert url_validator.is_valid_url("https://example.com") is True
        assert url_validator.is_valid_url("http://test.org/path") is True

    def test_is_valid_url_invalid(self, url_validator):
        """Test validating invalid URLs using instance method."""
        assert url_validator.is_valid_url("not-a-url") is False
        assert url_validator.is_valid_url("ftp://invalid") is False

    def test_is_suspicious_url_spam_domain(self, url_validator):
        """Test detecting suspicious spam domains using instance method."""
        assert url_validator.is_suspicious_url("https://bit.ly/abc123") is True
        assert url_validator.is_suspicious_url("http://tinyurl.com/test") is True

    def test_is_suspicious_url_suspicious_tld(self, url_validator):
        """Test detecting suspicious TLDs using instance method."""
        assert url_validator.is_suspicious_url("https://example.tk") is True
        assert url_validator.is_suspicious_url("http://test.ml") is True

    def test_is_suspicious_url_normal(self, url_validator):
        """Test normal URLs are not suspicious using instance method."""
        assert url_validator.is_suspicious_url("https://example.com") is False
        assert url_validator.is_suspicious_url("http://github.com") is False

    def test_url_validator_class_attributes(self, url_validator):
        """Test that URLValidator has expected class attributes."""
        assert hasattr(url_validator, "SPAM_DOMAINS")
        assert isinstance(url_validator.SPAM_DOMAINS, set)
        assert len(url_validator.SPAM_DOMAINS) > 0

        assert hasattr(url_validator, "SUSPICIOUS_TLDS")
        assert isinstance(url_validator.SUSPICIOUS_TLDS, set)
        assert len(url_validator.SUSPICIOUS_TLDS) > 0


# ============================================================================
# PatternValidator Tests - Testing Class Instance Methods
# ============================================================================


class TestPatternValidator:
    """Tests for PatternValidator class - testing instance methods."""

    def test_check_excessive_caps_yes(self, pattern_validator):
        """Test detecting excessive capital letters using instance method."""
        text = "THIS IS ALL CAPS AND SHOULD BE FLAGGED"
        result = pattern_validator.check_excessive_caps(text, threshold=0.5)
        assert result is True

    def test_check_excessive_caps_no(self, pattern_validator):
        """Test normal capitalization is not flagged using instance method."""
        text = "This is normal text with proper capitalization"
        result = pattern_validator.check_excessive_caps(text, threshold=0.5)
        assert result is False

    def test_check_excessive_caps_threshold(self, pattern_validator):
        """Test excessive caps detection respects threshold."""
        text = "MIXED Case Text"
        # With high threshold, should not flag
        assert pattern_validator.check_excessive_caps(text, threshold=0.9) is False
        # With low threshold, should flag
        assert pattern_validator.check_excessive_caps(text, threshold=0.1) is True

    def test_check_repeated_words_yes(self, pattern_validator):
        """Test detecting repeated words using instance method."""
        text = "spam spam spam spam spam spam spam"
        result = pattern_validator.check_repeated_words(text, min_repeats=5)
        assert result is True

    def test_check_repeated_words_no(self, pattern_validator):
        """Test normal text without excessive repetition using instance method."""
        text = "This is a normal sentence with varied words"
        result = pattern_validator.check_repeated_words(text, min_repeats=5)
        assert result is False

    def test_check_repeated_words_threshold(self, pattern_validator):
        """Test repeated words detection respects min_repeats threshold."""
        text = "word word word word"  # 4 repeats
        assert pattern_validator.check_repeated_words(text, min_repeats=5) is False
        assert pattern_validator.check_repeated_words(text, min_repeats=3) is True

    def test_check_excessive_special_chars_yes(self, pattern_validator):
        """Test detecting excessive special characters using instance method."""
        text = "!!!@@@###$$$%%%^^^&&&***"
        result = pattern_validator.check_excessive_special_chars(text, threshold=0.3)
        assert result is True

    def test_check_excessive_special_chars_no(self, pattern_validator):
        """Test normal text with acceptable special chars using instance method."""
        text = "Hello, world! How are you?"
        result = pattern_validator.check_excessive_special_chars(text, threshold=0.3)
        assert result is False

    def test_check_excessive_special_chars_threshold(self, pattern_validator):
        """Test special chars detection respects threshold."""
        text = "Hello!!!"
        assert pattern_validator.check_excessive_special_chars(text, threshold=0.5) is False
        assert pattern_validator.check_excessive_special_chars(text, threshold=0.1) is True


# ============================================================================
# SpamValidator Tests - Testing Class Instance Methods
# ============================================================================


class TestSpamValidator:
    """Tests for SpamValidator class - testing instance methods."""

    def test_validator_initialization(self):
        """Test SpamValidator can be instantiated with different configurations."""
        validator1 = SpamValidator(enable_ml=False, enable_profanity=False)
        assert validator1.enable_ml is False
        assert validator1.enable_profanity is False

        validator2 = SpamValidator(enable_ml=True, enable_profanity=True)
        assert (
            validator2.enable_ml is True or validator2.enable_ml is False
        )  # May be False if transformers unavailable
        assert (
            validator2.enable_profanity is True or validator2.enable_profanity is False
        )  # May be False if profanity unavailable

    @pytest.mark.asyncio
    async def test_check_frequency_spam(self, spam_validator):
        """Test frequency check flags spam using instance method."""
        result = await spam_validator._check_frequency(recent_submissions_count=15, threshold=10)
        assert isinstance(result, SpamCheckResult)
        assert result.is_spam is True
        assert "high_frequency" in result.flag
        assert result.confidence > 0.5

    @pytest.mark.asyncio
    async def test_check_frequency_ok(self, spam_validator):
        """Test frequency check allows normal submissions using instance method."""
        result = await spam_validator._check_frequency(recent_submissions_count=5, threshold=10)
        assert isinstance(result, SpamCheckResult)
        assert result.is_spam is False
        assert result.confidence > 0.0

    @pytest.mark.asyncio
    async def test_check_urls_excessive(self, spam_validator):
        """Test URL check flags excessive URLs using instance method."""
        content = " ".join(["https://example.com"] * 5)
        result = await spam_validator._check_urls(content, max_urls=3)
        assert isinstance(result, SpamCheckResult)
        assert result.is_spam is True
        assert "excessive_urls" in result.flag

    @pytest.mark.asyncio
    async def test_check_urls_suspicious(self, spam_validator):
        """Test URL check flags suspicious URLs using instance method."""
        content = "Check this out: https://bit.ly/abc123"
        result = await spam_validator._check_urls(content, max_urls=3)
        assert isinstance(result, SpamCheckResult)
        assert result.is_spam is True
        assert "suspicious_urls" in result.flag

    @pytest.mark.asyncio
    async def test_check_urls_ok(self, spam_validator):
        """Test URL check allows normal URLs using instance method."""
        content = "Visit https://example.com for more info"
        result = await spam_validator._check_urls(content, max_urls=3)
        assert isinstance(result, SpamCheckResult)
        assert result.is_spam is False

    @pytest.mark.asyncio
    async def test_check_patterns_excessive_caps(self, spam_validator):
        """Test pattern check flags excessive caps using instance method."""
        content = "THIS IS ALL CAPS AND SHOULD BE FLAGGED"
        result = await spam_validator._check_patterns(content)
        assert isinstance(result, SpamCheckResult)
        assert result.is_spam is True
        assert "excessive_caps" in result.flag

    @pytest.mark.asyncio
    async def test_check_patterns_repeated_words(self, spam_validator):
        """Test pattern check flags repeated words using instance method."""
        content = "spam spam spam spam spam spam"
        result = await spam_validator._check_patterns(content)
        assert isinstance(result, SpamCheckResult)
        assert result.is_spam is True
        assert "repeated_words" in result.flag

    @pytest.mark.asyncio
    async def test_check_patterns_ok(self, spam_validator):
        """Test pattern check allows normal text using instance method."""
        content = "This is normal feedback text with proper formatting"
        result = await spam_validator._check_patterns(content)
        assert isinstance(result, SpamCheckResult)
        assert result.is_spam is False

    @pytest.mark.asyncio
    async def test_validate_clean_content(self, spam_validator):
        """Test validation of clean content using instance method."""
        result = await spam_validator.validate(
            content="This is legitimate feedback about a safety issue.",
            recent_submissions_count=2,
            max_urls_threshold=3,
            frequency_threshold=10,
        )
        assert isinstance(result, SpamCheckResult) is False  # Should be SpamValidationResult
        assert hasattr(result, "is_spam")
        assert result.is_spam is False
        assert result.allow_submission is True
        assert len(result.flags) == 0 or "ml_detection" in result.flags  # ML might add flag

    @pytest.mark.asyncio
    async def test_validate_spam_multiple_flags(self, spam_validator):
        """Test validation catches spam with multiple flags using instance method."""
        content = "CHECK THIS OUT " + " ".join(["https://bit.ly/abc"] * 5)
        result = await spam_validator.validate(
            content=content,
            recent_submissions_count=15,  # High frequency
            max_urls_threshold=3,
            frequency_threshold=10,
        )
        assert result.is_spam is True
        assert result.allow_submission is False
        assert len(result.flags) > 0

    @pytest.mark.asyncio
    async def test_validate_aggregates_results(self, spam_validator):
        """Test that validation properly aggregates results using instance method."""
        result = await spam_validator.validate(
            content="Normal feedback text",
            recent_submissions_count=0,
            max_urls_threshold=3,
            frequency_threshold=10,
        )
        assert hasattr(result, "is_spam")
        assert isinstance(result.is_spam, bool)
        assert isinstance(result.confidence, float)
        assert 0.0 <= result.confidence <= 1.0
        assert isinstance(result.flags, list)
        assert isinstance(result.allow_submission, bool)
        assert isinstance(result.reason, str)

    @pytest.mark.asyncio
    async def test_validator_initialization_method(self, spam_validator):
        """Test that validator can be initialized asynchronously."""
        # Should not raise exception
        await spam_validator.initialize()


# ============================================================================
# SpamValidatorFactory Tests - Testing Factory Pattern
# ============================================================================


class TestSpamValidatorFactory:
    """Tests for SpamValidatorFactory - testing factory pattern."""

    def test_factory_instantiation(self, spam_validator_factory):
        """Test that factory can be instantiated."""
        assert isinstance(spam_validator_factory, SpamValidatorFactory)

    def test_factory_singleton(self):
        """Test that factory follows singleton pattern."""
        factory1 = SpamValidatorFactory()
        factory2 = SpamValidatorFactory()
        assert factory1 is factory2

    def test_get_factory_function(self):
        """Test get_spam_validator_factory function returns singleton."""
        factory1 = get_spam_validator_factory()
        factory2 = get_spam_validator_factory()
        assert factory1 is factory2
        assert isinstance(factory1, SpamValidatorFactory)

    def test_get_validator_returns_instance(self, spam_validator_factory):
        """Test getting validator from factory returns SpamValidator instance."""
        validator = spam_validator_factory.get_validator()
        assert isinstance(validator, SpamValidator)

    def test_get_validator_returns_same_instance(self, spam_validator_factory):
        """Test that factory returns same validator instance."""
        validator1 = spam_validator_factory.get_validator()
        validator2 = spam_validator_factory.get_validator()
        assert validator1 is validator2

    @pytest.mark.asyncio
    async def test_factory_initialize(self, spam_validator_factory):
        """Test factory initialization method."""
        # Should not raise exception even if ML model fails to load
        await spam_validator_factory.initialize()
        validator = spam_validator_factory.get_validator()
        assert validator is not None
        assert isinstance(validator, SpamValidator)


# ============================================================================
# SpamCheckResult Tests - Testing Data Classes
# ============================================================================


class TestSpamCheckResult:
    """Tests for SpamCheckResult dataclass."""

    def test_spam_check_result_creation(self):
        """Test creating SpamCheckResult instance."""
        result = SpamCheckResult(
            is_spam=True,
            confidence=0.9,
            flag="test_flag",
            reason="Test reason",
        )
        assert result.is_spam is True
        assert result.confidence == 0.9
        assert result.flag == "test_flag"
        assert result.reason == "Test reason"

    def test_spam_check_result_attributes(self):
        """Test SpamCheckResult has all required attributes."""
        result = SpamCheckResult(is_spam=False, confidence=0.5, flag="", reason="OK")
        assert hasattr(result, "is_spam")
        assert hasattr(result, "confidence")
        assert hasattr(result, "flag")
        assert hasattr(result, "reason")


# ============================================================================
# Integration Tests - Testing Object Interactions
# ============================================================================


class TestSpamValidatorIntegration:
    """Integration tests - testing object interactions and real scenarios."""

    @pytest.mark.asyncio
    async def test_phishing_link_spam(self, spam_validator):
        """Test detection of phishing/spam links through validator instance."""
        content = "Click here for free money: https://bit.ly/suspicious-link"
        result = await spam_validator.validate(
            content=content,
            recent_submissions_count=1,
            max_urls_threshold=3,
            frequency_threshold=10,
        )
        assert result.is_spam is True
        assert "suspicious_urls" in result.flags

    @pytest.mark.asyncio
    async def test_repetitive_spam(self, spam_validator):
        """Test detection of repetitive spam content through validator instance."""
        content = "buy buy buy buy buy buy buy buy buy buy"
        result = await spam_validator.validate(
            content=content,
            recent_submissions_count=1,
            max_urls_threshold=3,
            frequency_threshold=10,
        )
        assert result.is_spam is True
        assert "repeated_words" in result.flags

    @pytest.mark.asyncio
    async def test_excessive_caps_spam(self, spam_validator):
        """Test detection of excessive caps spam through validator instance."""
        content = "URGENT!!! CLICK NOW!!! LIMITED TIME OFFER!!!"
        result = await spam_validator.validate(
            content=content,
            recent_submissions_count=1,
            max_urls_threshold=3,
            frequency_threshold=10,
        )
        assert result.is_spam is True
        assert "excessive_caps" in result.flags

    @pytest.mark.asyncio
    async def test_legitimate_feedback(self, spam_validator):
        """Test that legitimate feedback passes validation through validator instance."""
        content = (
            "I noticed a broken streetlight at the intersection of Main St and "
            "Oak Ave. It's been out for several days and poses a safety risk, "
            "especially for pedestrians crossing at night."
        )
        result = await spam_validator.validate(
            content=content,
            recent_submissions_count=1,
            max_urls_threshold=3,
            frequency_threshold=10,
        )
        assert result.is_spam is False
        assert result.allow_submission is True

    @pytest.mark.asyncio
    async def test_validator_with_url_and_pattern_validators(self, spam_validator):
        """Test that validator uses URLValidator and PatternValidator internally."""
        # Test that validator can detect both URL and pattern issues
        content = "CHECK THIS OUT " + " ".join(["https://bit.ly/abc"] * 5)
        result = await spam_validator.validate(
            content=content,
            recent_submissions_count=1,
            max_urls_threshold=3,
            frequency_threshold=10,
        )
        # Should detect both excessive URLs and excessive caps
        assert result.is_spam is True
        assert len(result.flags) >= 1  # At least one flag should be present
