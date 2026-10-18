import json

import httpx
import pytest

from sdcmodeler.client import SDCStudioClient

BASE = "https://sdcstudio.test.local"
KEY = "sdcs_" + "a" * 48


class Fake:
    """A scripted SDCStudio: routes keyed by (method, path) -> (status, body or callable)."""

    def __init__(self):
        self.routes: dict = {}
        self.calls: list = []
        # assemble() checks the Modeler's default project once; most tests have one.
        self.on("GET", "/api/v1/auth/modeler/", body={"project_ct_id": "p-default", "projects": []})

    def on(self, method, path, status=200, body=None, headers=None):
        self.routes[(method, path)] = (status, body, headers or {})

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        key = (request.method, request.url.path)
        if request.url.host != "sdcstudio.test.local":
            key = (request.method, str(request.url))
        if key not in self.routes:
            return httpx.Response(404, json={"error": f"no route {key}"})
        status, body, headers = self.routes[key]
        if callable(body):
            body = body(request)
        if isinstance(body, (bytes, bytearray)):
            return httpx.Response(status, content=body, headers=headers)
        return httpx.Response(status, json=body, headers=headers)

    def client(self, api_key=KEY) -> SDCStudioClient:
        http = httpx.Client(transport=httpx.MockTransport(self.handler), base_url=BASE)
        return SDCStudioClient(base_url=BASE, api_key=api_key, http=http)


@pytest.fixture
def fake():
    return Fake()


def body_of(request: httpx.Request):
    return json.loads(request.content) if request.content else {}
