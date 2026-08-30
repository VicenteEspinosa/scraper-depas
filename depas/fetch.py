import random
import time
from typing import Any

from curl_cffi import requests

DELAY_RANGE = (0.8, 2.0)


class Fetcher:
    """Browser-impersonating HTTP session with retries and a polite delay."""

    def __init__(self, impersonate: str = "chrome", timeout: int = 30, retries: int = 3) -> None:
        self.session = requests.Session(impersonate=impersonate, timeout=timeout)
        self.retries = retries

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        return self._request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> requests.Response:
        return self._request("POST", url, **kwargs)

    def _request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        for attempt in range(self.retries):
            time.sleep(random.uniform(*DELAY_RANGE))
            response = self.session.request(method, url, **kwargs)
            if response.status_code < 500 and response.status_code != 429:
                response.raise_for_status()
                return response
            if attempt == self.retries - 1:
                response.raise_for_status()
            time.sleep(2**attempt)
        raise RuntimeError("unreachable")

    def close(self) -> None:
        self.session.close()
