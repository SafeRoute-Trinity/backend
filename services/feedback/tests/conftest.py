import pytest

import services.feedback.main as feedback_main


@pytest.fixture(autouse=True)
def disable_feedback_rate_limiter(monkeypatch):
    """
    Feedback service app has Redis-backed rate limiting middleware.
    In unit test runs Redis may be unavailable or counters may persist,
    which can make tests flaky (429 responses).

    For deterministic unit tests, we disable rate limiting by forcing
    the limiter to always allow requests.
    """

    async def _allow_all(_request):
        return None

    # `factory.create_app()` captures the RateLimiter instance into middleware closure.
    # Patching the instance method is sufficient.
    limiter = getattr(feedback_main, "factory", None).rate_limiter
    monkeypatch.setattr(limiter, "check", _allow_all)
    yield
