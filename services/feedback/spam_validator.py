"""
Spam Validator Module - Multi-layered spam detection for feedback content.

This module provides comprehensive spam detection using:
- ML-based text classification (BERT model)
- URL extraction and validation
- Pattern matching (repeated words, excessive links, character patterns)
- Profanity filtering
- Frequency-based checks

All checks run concurrently for optimal performance.
"""

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, List, Optional, Set, Tuple
from urllib.parse import urlparse

import validators

logger = logging.getLogger(__name__)

# Try to import transformers for ML model (optional dependency)
if TYPE_CHECKING:
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

try:
    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    TRANSFORMERS_AVAILABLE = True
except ImportError:
    TRANSFORMERS_AVAILABLE = False
    # Create type aliases for when transformers is not available
    AutoTokenizer = Any  # type: ignore
    AutoModelForSequenceClassification = Any  # type: ignore
    logger.warning("transformers library not available. ML-based spam detection will be disabled.")

# Try to import better-profanity (optional dependency)
try:
    from better_profanity import profanity

    PROFANITY_AVAILABLE = True
except ImportError:
    PROFANITY_AVAILABLE = False
    logger.warning("better-profanity library not available. Profanity filtering will be disabled.")


@dataclass
class SpamCheckResult:
    """Result of a single spam check."""

    is_spam: bool
    confidence: float
    flag: str
    reason: str


@dataclass
class SpamValidationResult:
    """Aggregated result of all spam checks."""

    is_spam: bool
    confidence: float
    flags: List[str]
    allow_submission: bool
    reason: str


class URLValidator:
    """Utility class for URL validation and extraction."""

    # Common spam URL patterns
    SPAM_DOMAINS: Set[str] = {
        "bit.ly",
        "tinyurl.com",
        "t.co",
        "goo.gl",
        "ow.ly",
        "is.gd",
        "buff.ly",
    }

    # Suspicious TLDs
    SUSPICIOUS_TLDS: Set[str] = {
        ".tk",
        ".ml",
        ".ga",
        ".cf",
        ".gq",
        ".top",
        ".xyz",
        ".click",
        ".download",
    }

    def extract_urls(self, text: str) -> List[str]:
        """
        Extract all URLs from text.

        Args:
            text: Input text to extract URLs from

        Returns:
            List of extracted URLs
        """
        # Pattern to match URLs
        url_pattern = re.compile(
            r"http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+"
        )
        urls = url_pattern.findall(text)
        return urls

    def is_valid_url(self, url: str) -> bool:
        """
        Check if URL is valid.

        Args:
            url: URL to validate

        Returns:
            True if URL is valid, False otherwise
        """
        return validators.url(url) is True

    def is_suspicious_url(self, url: str) -> bool:
        """
        Check if URL is suspicious (spam domain or suspicious TLD).

        Args:
            url: URL to check

        Returns:
            True if URL is suspicious, False otherwise
        """
        try:
            parsed = urlparse(url)
            domain = parsed.netloc.lower()

            # Check against spam domains
            for spam_domain in self.SPAM_DOMAINS:
                if spam_domain in domain:
                    return True

            # Check against suspicious TLDs
            for tld in self.SUSPICIOUS_TLDS:
                if domain.endswith(tld):
                    return True

            return False
        except Exception:
            return True  # If parsing fails, consider suspicious


class PatternValidator:
    """Utility class for pattern-based spam detection."""

    def check_excessive_caps(self, text: str, threshold: float = 0.5) -> bool:
        """
        Check if text has excessive capital letters.

        Args:
            text: Text to check
            threshold: Ratio of caps to total letters threshold (default: 0.5)

        Returns:
            True if excessive caps detected
        """
        if not text:
            return False

        letters = [c for c in text if c.isalpha()]
        if not letters:
            return False

        caps_count = sum(1 for c in letters if c.isupper())
        caps_ratio = caps_count / len(letters)

        return caps_ratio > threshold

    def check_repeated_words(self, text: str, min_repeats: int = 5) -> bool:
        """
        Check if text has excessive word repetition.

        Args:
            text: Text to check
            min_repeats: Minimum number of repeats to flag (default: 5)

        Returns:
            True if excessive repetition detected
        """
        words = text.lower().split()
        if not words:
            return False

        word_counts: dict[str, int] = {}
        for word in words:
            # Ignore very short words
            if len(word) > 2:
                word_counts[word] = word_counts.get(word, 0) + 1

        # Check if any word appears too many times
        max_count = max(word_counts.values()) if word_counts else 0
        return max_count >= min_repeats

    def check_excessive_special_chars(self, text: str, threshold: float = 0.3) -> bool:
        """
        Check if text has excessive special characters.

        Args:
            text: Text to check
            threshold: Ratio of special chars to total chars threshold (default: 0.3)

        Returns:
            True if excessive special chars detected
        """
        if not text:
            return False

        special_chars = sum(1 for c in text if not c.isalnum() and not c.isspace())
        total_chars = len(text)

        if total_chars == 0:
            return False

        special_ratio = special_chars / total_chars
        return special_ratio > threshold


class MLSpamDetector:
    """ML-based spam detector using Hugging Face transformers."""

    def __init__(self, model_name: str = "AntiSpamInstitute/spam-detector-bert-MoE-v2.2"):
        """
        Initialize ML spam detector.

        Args:
            model_name: Hugging Face model name
        """
        self.model_name = model_name
        self.tokenizer: Optional[Any] = None
        self.model: Optional[Any] = None
        self._initialized = False

    async def initialize(self) -> None:
        """Initialize the model (loads asynchronously)."""
        if not TRANSFORMERS_AVAILABLE:
            logger.warning("Transformers not available, ML detection disabled")
            return

        if self._initialized:
            return

        try:
            # Run model loading in executor to avoid blocking
            loop = asyncio.get_event_loop()
            self.tokenizer, self.model = await loop.run_in_executor(
                None,
                self._load_model,
            )
            self._initialized = True
            logger.info(f"ML spam detector initialized with model: {self.model_name}")
        except Exception as e:
            logger.error(f"Failed to initialize ML spam detector: {e}")
            self._initialized = False

    def _load_model(self) -> Tuple[Any, Any]:
        """Load model synchronously (called in executor)."""
        if not TRANSFORMERS_AVAILABLE:
            raise RuntimeError("Transformers library not available")
        tokenizer = AutoTokenizer.from_pretrained(self.model_name)
        model = AutoModelForSequenceClassification.from_pretrained(self.model_name)
        return tokenizer, model

    async def detect(self, text: str) -> SpamCheckResult:
        """
        Detect spam using ML model.

        Args:
            text: Text to check

        Returns:
            SpamCheckResult with detection result
        """
        if not self._initialized or not self.tokenizer or not self.model:
            # Fallback: return neutral result if ML not available
            return SpamCheckResult(
                is_spam=False,
                confidence=0.5,
                flag="ml_unavailable",
                reason="ML model not available",
            )

        try:
            # Run inference in executor to avoid blocking
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None,
                self._predict,
                text,
            )

            # Model returns logits, interpret them for binary classification
            if TRANSFORMERS_AVAILABLE:
                try:
                    is_tensor = isinstance(result, torch.Tensor)
                except (NameError, AttributeError, TypeError):
                    is_tensor = False

                if is_tensor:
                    # Handle tensor shape: [batch_size, num_classes] or [num_classes]
                    if result.dim() == 2:
                        # Batch dimension present: [1, 2] -> take first item
                        logits = result[0]
                    else:
                        # No batch dimension: [2]
                        logits = result

                    # Apply softmax to get probabilities
                    probabilities = torch.softmax(logits, dim=-1)

                    # Assuming binary classification: [not_spam, spam]
                    # If model has different label mapping, adjust accordingly
                    if len(probabilities) >= 2:
                        spam_prob = float(probabilities[1])  # Probability of spam class
                    else:
                        # Fallback: use first class if only one output
                        spam_prob = float(probabilities[0])

                    is_spam = spam_prob > 0.5
                    confidence = abs(spam_prob - 0.5) * 2  # Normalize to 0-1
                else:
                    # Fallback if result is not a tensor
                    is_spam = False
                    spam_prob = 0.5
                    confidence = 0.0
            else:
                # Fallback if result format is unexpected
                is_spam = False
                spam_prob = 0.5
                confidence = 0.0

            return SpamCheckResult(
                is_spam=is_spam,
                confidence=confidence,
                flag="ml_detection",
                reason=f"ML model prediction (spam probability: {spam_prob:.2f})",
            )
        except Exception as e:
            logger.error(f"ML spam detection failed: {e}", exc_info=True)
            return SpamCheckResult(
                is_spam=False,
                confidence=0.0,
                flag="ml_error",
                reason=f"ML detection error: {str(e)}",
            )

    def _predict(self, text: str) -> Optional[Any]:
        """
        Run model prediction synchronously (called in executor).

        Args:
            text: Text to classify

        Returns:
            Tensor with logits, or None if error
        """
        if not TRANSFORMERS_AVAILABLE or not self.tokenizer or not self.model:
            return None

        try:
            inputs = self.tokenizer(
                text,
                return_tensors="pt",
                truncation=True,
                max_length=512,
                padding=True,
            )
            with torch.no_grad():
                outputs = self.model(**inputs)
            return outputs.logits
        except Exception as e:
            logger.error(f"Model prediction failed: {e}", exc_info=True)
            return None


class SpamValidator:
    """Main spam validator class with multi-layered detection."""

    def __init__(
        self,
        ml_detector: Optional[MLSpamDetector] = None,
        enable_ml: bool = True,
        enable_profanity: bool = True,
        url_validator: Optional[URLValidator] = None,
        pattern_validator: Optional[PatternValidator] = None,
    ):
        """
        Initialize spam validator.

        Args:
            ml_detector: Optional ML detector instance
            enable_ml: Enable ML-based detection
            enable_profanity: Enable profanity filtering
            url_validator: Optional URLValidator instance (creates new if None)
            pattern_validator: Optional PatternValidator instance (creates new if None)
        """
        self.ml_detector = ml_detector or (MLSpamDetector() if enable_ml else None)
        self.enable_ml = enable_ml and TRANSFORMERS_AVAILABLE
        self.enable_profanity = enable_profanity and PROFANITY_AVAILABLE

        # Create validator instances for TDD-friendly object-oriented approach
        self.url_validator = url_validator or URLValidator()
        self.pattern_validator = pattern_validator or PatternValidator()

        # Initialize profanity checker if enabled (will be loaded in executor)
        if self.enable_profanity:
            try:
                # Load profanity words synchronously during init
                profanity.load_censor_words()
            except Exception as e:
                logger.warning(f"Failed to load profanity words: {e}")
                self.enable_profanity = False

    async def initialize(self) -> None:
        """Initialize validator (loads ML model if enabled)."""
        if self.ml_detector and self.enable_ml:
            await self.ml_detector.initialize()

    async def validate(
        self,
        content: str,
        recent_submissions_count: int = 0,
        max_urls_threshold: int = 3,
        frequency_threshold: int = 10,
    ) -> SpamValidationResult:
        """
        Validate content for spam using multiple detection methods.

        Args:
            content: Content to validate
            recent_submissions_count: Number of recent submissions by user
            max_urls_threshold: Maximum allowed URLs before flagging
            frequency_threshold: Maximum submissions before flagging

        Returns:
            SpamValidationResult with aggregated results
        """
        # Run all checks concurrently for performance
        check_tasks = [
            self._check_frequency(recent_submissions_count, frequency_threshold),
            self._check_urls(content, max_urls_threshold),
            self._check_patterns(content),
        ]

        # Add optional checks
        if self.enable_profanity:
            check_tasks.append(self._check_profanity(content))
        if self.enable_ml and self.ml_detector:
            check_tasks.append(self._check_ml(content))

        checks = await asyncio.gather(*check_tasks, return_exceptions=True)

        # Process results
        results: List[SpamCheckResult] = []
        for check in checks:
            if isinstance(check, SpamCheckResult):
                results.append(check)
            elif isinstance(check, Exception):
                logger.error(f"Spam check failed: {check}")

        # Aggregate results
        return self._aggregate_results(results)

    async def _check_frequency(
        self, recent_submissions_count: int, threshold: int
    ) -> SpamCheckResult:
        """Check submission frequency."""
        is_spam = recent_submissions_count > threshold
        return SpamCheckResult(
            is_spam=is_spam,
            confidence=0.95 if is_spam else 0.9,
            flag="high_frequency" if is_spam else "",
            reason="Too many submissions" if is_spam else "OK",
        )

    async def _check_urls(self, content: str, max_urls: int) -> SpamCheckResult:
        """
        Check URLs in content (runs URL validation concurrently).

        Args:
            content: Content to check
            max_urls: Maximum allowed URLs

        Returns:
            SpamCheckResult with URL validation result
        """
        urls = self.url_validator.extract_urls(content)

        if len(urls) > max_urls:
            return SpamCheckResult(
                is_spam=True,
                confidence=0.9,
                flag="excessive_urls",
                reason=f"Too many URLs ({len(urls)} > {max_urls})",
            )

        if not urls:
            return SpamCheckResult(
                is_spam=False,
                confidence=0.9,
                flag="",
                reason="No URLs found",
            )

        # Run URL validations concurrently for better performance
        loop = asyncio.get_event_loop()
        validation_tasks = [
            loop.run_in_executor(None, self.url_validator.is_suspicious_url, url) for url in urls
        ]
        suspicious_results = await asyncio.gather(*validation_tasks)

        suspicious_count = sum(1 for is_suspicious in suspicious_results if is_suspicious)
        if suspicious_count > 0:
            return SpamCheckResult(
                is_spam=True,
                confidence=0.85,
                flag="suspicious_urls",
                reason=f"Found {suspicious_count} suspicious URL(s)",
            )

        # Check for invalid URLs concurrently
        validation_tasks = [
            loop.run_in_executor(None, self.url_validator.is_valid_url, url) for url in urls
        ]
        valid_results = await asyncio.gather(*validation_tasks)

        invalid_count = sum(1 for is_valid in valid_results if not is_valid)
        if invalid_count > 0:
            return SpamCheckResult(
                is_spam=True,
                confidence=0.8,
                flag="invalid_urls",
                reason=f"Found {invalid_count} invalid URL(s)",
            )

        return SpamCheckResult(
            is_spam=False,
            confidence=0.9,
            flag="",
            reason="URLs OK",
        )

    async def _check_profanity(self, content: str) -> SpamCheckResult:
        """
        Check for profanity (runs in executor for non-blocking).

        Args:
            content: Content to check

        Returns:
            SpamCheckResult with profanity detection result
        """
        if not self.enable_profanity:
            return SpamCheckResult(
                is_spam=False,
                confidence=0.0,
                flag="",
                reason="Profanity check disabled",
            )

        try:
            # Run profanity check in executor to avoid blocking
            loop = asyncio.get_event_loop()
            contains_profanity = await loop.run_in_executor(
                None,
                profanity.contains_profanity,
                content,
            )
            return SpamCheckResult(
                is_spam=contains_profanity,
                confidence=0.9 if contains_profanity else 0.8,
                flag="profanity" if contains_profanity else "",
                reason="Contains profanity" if contains_profanity else "No profanity detected",
            )
        except Exception as e:
            logger.error(f"Profanity check failed: {e}", exc_info=True)
            return SpamCheckResult(
                is_spam=False,
                confidence=0.0,
                flag="profanity_error",
                reason=f"Profanity check error: {str(e)}",
            )

    async def _check_patterns(self, content: str) -> SpamCheckResult:
        """
        Check for spam patterns (runs pattern checks concurrently).

        Args:
            content: Content to check

        Returns:
            SpamCheckResult with pattern detection result
        """
        # Run pattern checks concurrently for better performance
        loop = asyncio.get_event_loop()
        pattern_tasks = [
            loop.run_in_executor(None, self.pattern_validator.check_excessive_caps, content),
            loop.run_in_executor(None, self.pattern_validator.check_repeated_words, content),
            loop.run_in_executor(
                None, self.pattern_validator.check_excessive_special_chars, content
            ),
        ]
        results = await asyncio.gather(*pattern_tasks)

        flags: List[str] = []
        confidence_scores: List[float] = []

        # Process results
        if results[0]:  # Excessive caps
            flags.append("excessive_caps")
            confidence_scores.append(0.7)

        if results[1]:  # Repeated words
            flags.append("repeated_words")
            confidence_scores.append(0.75)

        if results[2]:  # Excessive special chars
            flags.append("excessive_special_chars")
            confidence_scores.append(0.7)

        if flags:
            return SpamCheckResult(
                is_spam=True,
                confidence=max(confidence_scores) if confidence_scores else 0.7,
                flag=",".join(flags),
                reason=f"Pattern detection: {', '.join(flags)}",
            )

        return SpamCheckResult(
            is_spam=False,
            confidence=0.85,
            flag="",
            reason="No spam patterns detected",
        )

    async def _check_ml(self, content: str) -> SpamCheckResult:
        """Check using ML model."""
        if not self.ml_detector:
            return SpamCheckResult(
                is_spam=False,
                confidence=0.0,
                flag="ml_disabled",
                reason="ML detection disabled",
            )

        return await self.ml_detector.detect(content)

    def _aggregate_results(self, results: List[SpamCheckResult]) -> SpamValidationResult:
        """
        Aggregate multiple spam check results.

        Args:
            results: List of individual check results

        Returns:
            Aggregated SpamValidationResult
        """
        if not results:
            return SpamValidationResult(
                is_spam=False,
                confidence=0.5,
                flags=[],
                allow_submission=True,
                reason="No checks performed",
            )

        # Collect all flags
        all_flags: List[str] = []
        spam_results: List[SpamCheckResult] = []
        ham_results: List[SpamCheckResult] = []

        for result in results:
            if result.flag:
                all_flags.append(result.flag)

            if result.is_spam:
                spam_results.append(result)
            else:
                ham_results.append(result)

        # Determine if spam based on any positive detection
        is_spam = len(spam_results) > 0

        # Calculate weighted confidence
        if spam_results:
            # Use highest confidence from spam results
            confidence = max(r.confidence for r in spam_results)
        else:
            # Use average confidence from ham results
            confidence = (
                sum(r.confidence for r in ham_results) / len(ham_results) if ham_results else 0.5
            )

        # Generate reason
        if is_spam:
            reasons = [r.reason for r in spam_results if r.reason]
            reason = "; ".join(reasons[:3])  # Limit to first 3 reasons
        else:
            reason = "Content validated successfully"

        return SpamValidationResult(
            is_spam=is_spam,
            confidence=confidence,
            flags=all_flags,
            allow_submission=not is_spam,
            reason=reason,
        )


class SpamValidatorFactory:
    """
    Factory for creating and managing SpamValidator instances.

    Follows singleton pattern similar to DatabaseFactory in libs/db.py.
    """

    _instance: Optional["SpamValidatorFactory"] = None
    _validator: Optional[SpamValidator] = None
    _initialized: bool = False

    def __new__(cls):
        """Singleton pattern - ensures only one factory instance exists."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        """Initialize factory (only runs once due to singleton)."""
        if not hasattr(self, "_initialized_instance"):
            self._initialized_instance = True
            self._validator = None
            self._initialized = False

    async def initialize(self) -> None:
        """
        Initialize the validator (loads ML model if enabled).

        This should be called during application startup.
        """
        if self._initialized:
            return

        if self._validator is None:
            self._validator = SpamValidator(
                enable_ml=True,
                enable_profanity=True,
            )

        await self._validator.initialize()
        self._initialized = True
        logger.info("SpamValidatorFactory initialized successfully")

    def get_validator(self) -> SpamValidator:
        """
        Get the spam validator instance.

        Returns:
            SpamValidator instance (creates one if not exists)

        Raises:
            RuntimeError: If factory not initialized
        """
        if self._validator is None:
            self._validator = SpamValidator(
                enable_ml=True,
                enable_profanity=True,
            )
        return self._validator


# Global factory instance (similar to DatabaseFactory pattern)
_spam_validator_factory: Optional[SpamValidatorFactory] = None


def get_spam_validator_factory() -> SpamValidatorFactory:
    """
    Get or create the global spam validator factory instance.

    Follows the same pattern as get_database_factory() in libs/db.py.

    Returns:
        SpamValidatorFactory instance
    """
    global _spam_validator_factory
    if _spam_validator_factory is None:
        _spam_validator_factory = SpamValidatorFactory()
    return _spam_validator_factory
