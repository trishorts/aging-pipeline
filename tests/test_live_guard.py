"""OFFLINE check of the outage rule itself: an outage must skip, and a contract break must still fail.
A guard that skipped too much would hide real failures behind a green CI."""
import io, socket, urllib.error

import pytest
import pymzlib
from _pytest.outcomes import Skipped

from live_guard import external_service


def http_error(code):
    return urllib.error.HTTPError("https://example.org", code, "msg", {}, io.BytesIO())


@pytest.mark.parametrize("exc", [
    pymzlib.ServiceUnavailableError("HttpRequestException", "PRIDE returned 503"),
    http_error(503), http_error(500), http_error(429), http_error(408),
    urllib.error.URLError("name resolution failed"), socket.timeout("timed out"), ConnectionResetError(),
])
def test_outages_skip(exc):
    with pytest.raises(Skipped, match="not a code failure"):
        with external_service("svc"):
            raise exc


@pytest.mark.parametrize("exc", [http_error(400), http_error(404), KeyError("accession"),
                                 AssertionError("wrong answer"), pymzlib.BridgeTimeoutError("bridge hung")])
def test_contract_breaks_fail(exc):
    with pytest.raises(type(exc)):
        with external_service("svc"):
            raise exc
