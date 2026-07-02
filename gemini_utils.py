"""
gemini_utils.py
Retry/backoff wrapper for Gemini API calls.

Why this exists: free-tier Gemini keys hit per-minute and per-day quota
limits fast, especially when an agent makes several tool-calling round
trips per user request. Without retry logic, one 429 kills the whole
itinerary generation. This wraps every model call with exponential
backoff + jitter, and fails gracefully (returns a clear error the UI
can show) after max retries instead of crashing.
"""

import time
import random
import functools
import google.generativeai as genai
from google.api_core.exceptions import ResourceExhausted, ServiceUnavailable, InternalServerError


class GeminiRateLimitError(Exception):
    """Raised when retries are exhausted due to persistent rate limiting."""
    pass


def with_retry(max_retries: int = 5, base_delay: float = 2.0, max_delay: float = 60.0):
    """
    Decorator: retries a Gemini API call on transient errors (429 quota,
    503 overloaded, 500 internal) with exponential backoff + jitter.

    Usage:
        @with_retry(max_retries=5)
        def call_model(prompt):
            return model.generate_content(prompt)
    """
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(max_retries):
                try:
                    return fn(*args, **kwargs)
                except (ResourceExhausted, ServiceUnavailable, InternalServerError) as e:
                    last_exc = e
                    delay = min(base_delay * (2 ** attempt), max_delay)
                    delay += random.uniform(0, delay * 0.3)  # jitter
                    if attempt < max_retries - 1:
                        time.sleep(delay)
                    continue
            raise GeminiRateLimitError(
                f"Gemini API unavailable after {max_retries} retries: {last_exc}"
            )
        return wrapper
    return decorator


def safe_generate(model, prompt, max_retries=5):
    """One-shot helper: call model.generate_content with retry, return text or raise."""
    @with_retry(max_retries=max_retries)
    def _call():
        return model.generate_content(prompt)

    response = _call()
    return response.text
