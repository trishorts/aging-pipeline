"""The outage rule for live tests, shared by tests/test_live.py and checked offline by test_live_guard.py.

Same contract as mzLib's ExternalServiceTestHelper and pyMzLib's conftest: a service OUTAGE (timeout, refused
connection, HTTP 408/429/5xx, or pymzlib.ServiceUnavailableError) skips; every other error fails.
"""
import contextlib, socket, urllib.error

import pytest
import pymzlib

def _is_outage_status(code: int) -> bool:
    return code in (408, 429) or code >= 500


@contextlib.contextmanager
def external_service(name: str):
    """Skip on an outage of `name`; let every other error fail the test."""
    try:
        yield
    except pymzlib.ServiceUnavailableError as e:
        pytest.skip(f"{name} unavailable ({e}). A third-party availability problem, not a code failure.")
    except urllib.error.HTTPError as e:
        if _is_outage_status(e.code):
            pytest.skip(f"{name} returned HTTP {e.code}. A third-party availability problem, not a code failure.")
        raise
    except (urllib.error.URLError, socket.timeout, ConnectionError, TimeoutError) as e:
        pytest.skip(f"{name} unreachable ({e}). A third-party availability problem, not a code failure.")
