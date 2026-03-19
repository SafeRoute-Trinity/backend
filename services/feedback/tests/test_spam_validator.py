import asyncio

import pytest

import services.feedback.spam_validator as spam_validator


def _run(coro):
    return asyncio.run(coro)


def test_url_validator_extract_urls_finds_http_urls():
    uv = spam_validator.URLValidator()
    urls = uv.extract_urls("hello http://example.com/test and https://a.co/x")
    assert "http://example.com/test" in urls
    assert "https://a.co/x" in urls


def test_url_validator_is_suspicious_url_spam_domain_and_tld():
    uv = spam_validator.URLValidator()

    assert uv.is_suspicious_url("http://bit.ly/abc")
    assert uv.is_suspicious_url("https://example.xyz/path")


def test_url_validator_is_suspicious_url_exception_returns_true(monkeypatch):
    uv = spam_validator.URLValidator()

    def _bad_urlparse(_url):
        raise ValueError("parse failed")

    monkeypatch.setattr(spam_validator, "urlparse", _bad_urlparse)
    assert uv.is_suspicious_url("http://example.com")


def test_pattern_validator_caps_repeated_words_and_special_chars():
    pv = spam_validator.PatternValidator()

    assert pv.check_excessive_caps("ABCDEFGH") is True
    assert pv.check_excessive_caps("") is False

    # Short words (len<=2) are ignored by check_repeated_words
    assert pv.check_repeated_words("hi hi hi hi hi") is False
    assert pv.check_repeated_words("spam spam spam spam spam") is True

    assert pv.check_excessive_special_chars("!!!???###") is True
    assert pv.check_excessive_special_chars("") is False


def test_ml_detector_returns_neutral_when_transformers_unavailable(monkeypatch):
    monkeypatch.setattr(spam_validator, "TRANSFORMERS_AVAILABLE", False)

    md = spam_validator.MLSpamDetector()
    res = _run(md.detect("hello world"))

    assert res.is_spam is False
    assert res.flag == "ml_unavailable"
    assert res.confidence == 0.5

    _run(md.initialize())
    assert md._initialized is False


def test_ml_detector_detect_handles_predict_exception(monkeypatch):
    monkeypatch.setattr(spam_validator, "TRANSFORMERS_AVAILABLE", False)

    md = spam_validator.MLSpamDetector()
    md._initialized = True
    md.tokenizer = object()
    md.model = object()

    def _predict(_text):
        raise RuntimeError("boom")

    md._predict = _predict
    res = _run(md.detect("hello"))

    assert res.is_spam is False
    assert res.flag == "ml_error"
    assert "ML detection error" in res.reason


def test_spam_validator_validate_frequency_branch_is_spam_when_gt_threshold():
    validator = spam_validator.SpamValidator(enable_ml=False, enable_profanity=False)

    res = _run(
        validator.validate(
            content="hello no urls",
            recent_submissions_count=11,
            max_urls_threshold=3,
            frequency_threshold=10,
        )
    )

    assert res.is_spam is True
    assert res.allow_submission is False
    assert "high_frequency" in res.flags
    assert res.confidence == 0.95
    assert "Too many submissions" in res.reason


def test_spam_validator_validate_frequency_branch_not_spam_when_equal_threshold():
    validator = spam_validator.SpamValidator(enable_ml=False, enable_profanity=False)

    res = _run(
        validator.validate(
            content="hello no urls",
            recent_submissions_count=10,
            max_urls_threshold=3,
            frequency_threshold=10,
        )
    )

    assert res.is_spam is False
    assert res.allow_submission is True
    assert res.flags == []
    assert res.reason == "Content validated successfully"


def test_spam_validator_validate_urls_excessive_urls():
    validator = spam_validator.SpamValidator(enable_ml=False, enable_profanity=False)

    content = " ".join(
        [
            "http://example.com/1",
            "http://example.com/2",
            "http://example.com/3",
            "http://example.com/4",
        ]
    )

    res = _run(
        validator.validate(
            content=content,
            recent_submissions_count=0,
            max_urls_threshold=3,
            frequency_threshold=10,
        )
    )

    assert res.is_spam is True
    assert "excessive_urls" in res.flags
    assert res.allow_submission is False


def test_spam_validator_validate_urls_suspicious_urls(monkeypatch):
    validator = spam_validator.SpamValidator(enable_ml=False, enable_profanity=False)

    def _is_suspicious(url: str) -> bool:
        return url.endswith("/bad")

    def _is_valid(url: str) -> bool:
        return True

    monkeypatch.setattr(validator.url_validator, "is_suspicious_url", _is_suspicious)
    monkeypatch.setattr(validator.url_validator, "is_valid_url", _is_valid)

    res = _run(
        validator.validate(
            content="http://example.com/good http://example.com/bad",
            recent_submissions_count=0,
            max_urls_threshold=5,
            frequency_threshold=10,
        )
    )

    assert res.is_spam is True
    assert "suspicious_urls" in res.flags


def test_spam_validator_validate_urls_invalid_urls(monkeypatch):
    validator = spam_validator.SpamValidator(enable_ml=False, enable_profanity=False)

    monkeypatch.setattr(validator.url_validator, "is_suspicious_url", lambda _u: False)
    monkeypatch.setattr(validator.url_validator, "is_valid_url", lambda u: not u.endswith("/x"))

    res = _run(
        validator.validate(
            content="http://example.com/ok http://example.com/x",
            recent_submissions_count=0,
            max_urls_threshold=5,
            frequency_threshold=10,
        )
    )

    assert res.is_spam is True
    assert "invalid_urls" in res.flags


def test_spam_validator_validate_patterns_repeated_words(monkeypatch):
    validator = spam_validator.SpamValidator(enable_ml=False, enable_profanity=False)

    monkeypatch.setattr(validator.pattern_validator, "check_excessive_caps", lambda _c: False)
    monkeypatch.setattr(validator.pattern_validator, "check_repeated_words", lambda _c: True)
    monkeypatch.setattr(
        validator.pattern_validator, "check_excessive_special_chars", lambda _c: False
    )

    res = _run(
        validator.validate(
            content="whatever",
            recent_submissions_count=0,
            max_urls_threshold=0,
            frequency_threshold=10,
        )
    )

    assert res.is_spam is True
    assert "repeated_words" in ",".join(res.flags) or "repeated_words" in res.flags


def test_spam_validator_validate_when_all_checks_raise_returns_no_checks_performed(monkeypatch):
    validator = spam_validator.SpamValidator(enable_ml=False, enable_profanity=False)

    async def _raise(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(validator, "_check_frequency", _raise)
    monkeypatch.setattr(validator, "_check_urls", _raise)
    monkeypatch.setattr(validator, "_check_patterns", _raise)

    res = _run(
        validator.validate(
            content="anything",
            recent_submissions_count=0,
            max_urls_threshold=3,
            frequency_threshold=10,
        )
    )

    assert res.is_spam is False
    assert res.allow_submission is True
    assert res.flags == []
    assert res.reason == "No checks performed"


def test_spam_validator_check_profanity_disabled_branch():
    validator = spam_validator.SpamValidator(enable_ml=False, enable_profanity=False)

    res = _run(validator._check_profanity("hello"))
    assert res.is_spam is False
    assert res.confidence == 0.0
    assert res.reason == "Profanity check disabled"


def test_spam_validator_check_profanity_exception_branch(monkeypatch):
    validator = spam_validator.SpamValidator(enable_ml=False, enable_profanity=False)

    class _Profanity:
        @staticmethod
        def contains_profanity(_content):
            raise RuntimeError("profanity backend down")

    # Force enable_profanity branch after init so we can hit the try/except.
    validator.enable_profanity = True
    monkeypatch.setattr(spam_validator, "profanity", _Profanity, raising=False)

    res = _run(validator._check_profanity("hello"))
    assert res.flag == "profanity_error"
    assert "Profanity check error" in res.reason


def test_spam_validator_check_ml_disabled_and_enabled(monkeypatch):
    # Disabled branch when ml_detector is None
    validator = spam_validator.SpamValidator(enable_ml=False, enable_profanity=False)
    res = _run(validator._check_ml("hello"))
    assert res.flag == "ml_disabled"

    class _DummyML:
        async def detect(self, content: str):
            return spam_validator.SpamCheckResult(
                is_spam=True,
                confidence=0.3,
                flag="ml_detection",
                reason="dummy",
            )

    validator.ml_detector = _DummyML()
    validator.enable_ml = True
    res2 = _run(validator._check_ml("hello"))
    assert res2.is_spam is True
    assert res2.flag == "ml_detection"


def test_url_validator_is_valid_url_true_and_false():
    uv = spam_validator.URLValidator()
    assert uv.is_valid_url("http://example.com/path") is True
    assert uv.is_valid_url("not-a-url") is False


def test_url_validator_is_suspicious_url_false_for_clean_url():
    uv = spam_validator.URLValidator()
    assert uv.is_suspicious_url("http://example.com/safe") is False


def test_pattern_validator_caps_false_when_no_alpha_chars():
    pv = spam_validator.PatternValidator()
    # Contains letters count = 0 -> returns False on caps branch.
    assert pv.check_excessive_caps("12345!!!!") is False


def test_pattern_validator_repeated_words_false_when_no_words():
    pv = spam_validator.PatternValidator()
    assert pv.check_repeated_words("   ") is False


def test_ml_detector_initialize_success_path_short_circuit_when_already_initialized(monkeypatch):
    monkeypatch.setattr(spam_validator, "TRANSFORMERS_AVAILABLE", True)
    md = spam_validator.MLSpamDetector()
    md._initialized = True
    # Should short-circuit before calling _load_model.
    _run(md.initialize())
    assert md._initialized is True


def test_ml_detector_initialize_exception_sets_initialized_false(monkeypatch):
    monkeypatch.setattr(spam_validator, "TRANSFORMERS_AVAILABLE", True)
    md = spam_validator.MLSpamDetector()
    md._initialized = False

    def _load_model_raises():
        raise RuntimeError("load failed")

    md._load_model = _load_model_raises
    _run(md.initialize())

    assert md._initialized is False


def test_ml_detector_load_model_raises_when_transformers_unavailable(monkeypatch):
    monkeypatch.setattr(spam_validator, "TRANSFORMERS_AVAILABLE", False)
    md = spam_validator.MLSpamDetector()
    with pytest.raises(RuntimeError, match="Transformers library not available"):
        md._load_model()


def test_ml_detector_detect_tensor_path(monkeypatch):
    """
    Cover MLSpamDetector.detect branch where:
    - result is treated as torch.Tensor
    - probabilities length >= 2
    - is_spam depends on spam_prob > 0.5
    """
    monkeypatch.setattr(spam_validator, "TRANSFORMERS_AVAILABLE", True)

    # Fake torch module to avoid real torch ops.
    class _FakeTensor:
        def __init__(self, probs):
            # logits/probs container; we don't care about semantics for coverage.
            self._probs = probs

        def dim(self):
            return 1

        def __getitem__(self, idx):
            return self

    class _FakeTorch:
        class Tensor(_FakeTensor):
            pass

        @staticmethod
        def softmax(logits, dim=-1):
            # Return something indexable by [1]
            return [0.1, 0.9]

    monkeypatch.setattr(spam_validator, "torch", _FakeTorch, raising=False)

    md = spam_validator.MLSpamDetector()
    md._initialized = True
    md.tokenizer = object()
    md.model = object()

    # Force _predict to return a fake tensor instance.
    md._predict = lambda _text: _FakeTorch.Tensor([0.1, 0.9])

    res = _run(md.detect("hello"))
    assert res.flag == "ml_detection"
    assert res.is_spam is True


def test_ml_detector_predict_early_return_and_exception(monkeypatch):
    monkeypatch.setattr(spam_validator, "TRANSFORMERS_AVAILABLE", False)
    md = spam_validator.MLSpamDetector()
    assert md._predict("x") is None

    monkeypatch.setattr(spam_validator, "TRANSFORMERS_AVAILABLE", True)

    # Provide minimal torch API so _predict can enter try/except deterministically.
    class _NoGrad:
        def __enter__(self):
            return None

        def __exit__(self, exc_type, exc, tb):
            return False

    class _FakeTorch:
        @staticmethod
        def no_grad():
            return _NoGrad()

    monkeypatch.setattr(spam_validator, "torch", _FakeTorch, raising=False)

    md2 = spam_validator.MLSpamDetector()
    md2.tokenizer = lambda *args, **kwargs: {}
    md2.model = object()

    # Make model(**inputs) fail by providing model without __call__
    assert md2._predict("x") is None


def test_spam_validator_initialize_loads_ml_detector_when_enabled(monkeypatch):
    class _DummyML:
        def __init__(self):
            self.initialized = False

        async def initialize(self):
            self.initialized = True

    monkeypatch.setattr(spam_validator, "TRANSFORMERS_AVAILABLE", True)
    validator = spam_validator.SpamValidator(
        ml_detector=_DummyML(), enable_ml=True, enable_profanity=False
    )
    _run(validator.initialize())
    assert validator.ml_detector.initialized is True


def test_spam_validator_init_profanity_load_exception_disables_profanity(monkeypatch):
    # Force enable_profanity branch and make profanity loader throw.
    monkeypatch.setattr(spam_validator, "PROFANITY_AVAILABLE", True)

    class _DummyProfanity:
        @staticmethod
        def load_censor_words():
            raise RuntimeError("bad dictionary")

        @staticmethod
        def contains_profanity(_c):
            return False

    monkeypatch.setattr(spam_validator, "profanity", _DummyProfanity, raising=False)
    validator = spam_validator.SpamValidator(enable_ml=False, enable_profanity=True)
    # __init__ should catch loader error and set enable_profanity=False.
    assert validator.enable_profanity is False


def test_spam_validator_validate_includes_ml_check_when_enabled(monkeypatch):
    monkeypatch.setattr(spam_validator, "TRANSFORMERS_AVAILABLE", True)

    class _DummyML:
        async def detect(self, content: str):
            return spam_validator.SpamCheckResult(
                is_spam=True,
                confidence=0.42,
                flag="ml_detection",
                reason="ML says spam",
            )

    validator = spam_validator.SpamValidator(
        ml_detector=_DummyML(), enable_ml=True, enable_profanity=False
    )
    res = _run(
        validator.validate(
            content="hello",
            recent_submissions_count=0,
            max_urls_threshold=10,
            frequency_threshold=10,
        )
    )
    assert res.is_spam is True
    assert "ml_detection" in res.flags


def test_spam_validator_urls_ok_branch(monkeypatch):
    validator = spam_validator.SpamValidator(enable_ml=False, enable_profanity=False)

    monkeypatch.setattr(validator.url_validator, "is_suspicious_url", lambda _u: False)
    monkeypatch.setattr(validator.url_validator, "is_valid_url", lambda _u: True)

    res = _run(
        validator.validate(
            content="http://example.com/ok",
            recent_submissions_count=0,
            max_urls_threshold=3,
            frequency_threshold=10,
        )
    )

    assert res.is_spam is False
    assert res.allow_submission is True


def test_spam_validator_patterns_excessive_caps_and_special_chars(monkeypatch):
    validator = spam_validator.SpamValidator(enable_ml=False, enable_profanity=False)

    # caps -> True, repeated -> False, special chars -> False
    monkeypatch.setattr(validator.pattern_validator, "check_excessive_caps", lambda _c: True)
    monkeypatch.setattr(validator.pattern_validator, "check_repeated_words", lambda _c: False)
    monkeypatch.setattr(
        validator.pattern_validator, "check_excessive_special_chars", lambda _c: False
    )

    res = _run(
        validator.validate(
            content="x",
            recent_submissions_count=0,
            max_urls_threshold=10,
            frequency_threshold=10,
        )
    )
    assert res.is_spam is True
    assert "excessive_caps" in res.flags

    # Now special chars -> True
    monkeypatch.setattr(validator.pattern_validator, "check_excessive_caps", lambda _c: False)
    monkeypatch.setattr(validator.pattern_validator, "check_repeated_words", lambda _c: False)
    monkeypatch.setattr(
        validator.pattern_validator, "check_excessive_special_chars", lambda _c: True
    )

    res2 = _run(
        validator.validate(
            content="x",
            recent_submissions_count=0,
            max_urls_threshold=10,
            frequency_threshold=10,
        )
    )
    assert res2.is_spam is True
    assert "excessive_special_chars" in res2.flags


def test_spam_validator_factory_singleton_initialize_and_get_validator(monkeypatch):
    # Reset singleton for deterministic tests
    spam_validator._spam_validator_factory = None

    factory1 = spam_validator.get_spam_validator_factory()
    factory2 = spam_validator.get_spam_validator_factory()
    assert factory1 is factory2

    # Patch internal validator initialize so we can assert it's called.
    class _DummyValidator:
        def __init__(self):
            self.inited = False

        async def initialize(self):
            self.inited = True

    dummy = _DummyValidator()
    factory1._validator = dummy

    _run(factory1.initialize())
    assert dummy.inited is True
