"""Explicit client-certificate transport for the unchanged upstream janitor suite."""

import base64
import os
import ssl
import tempfile
from pathlib import Path

import httpx
import pytest
import yaml
from kind_slow import KUBE
from kind_slow_config import CONTEXT
from lightkube import Client
from lightkube.config.kubeconfig import KubeConfig


def authenticated_client():
    if os.environ.get("OSDC_KIND_SLOW_KUBECONFIG") != str(KUBE):
        raise ValueError("Use the isolated kind slow-test entrypoint")
    config = yaml.safe_load(KUBE.read_text())
    if config["current-context"] != CONTEXT:
        raise ValueError("Unexpected kubeconfig context")
    cluster = next(c["cluster"] for c in config["clusters"] if c["name"] == CONTEXT)
    user = next(u["user"] for u in config["users"] if u["name"] == CONTEXT)
    context = ssl.create_default_context(cadata=base64.b64decode(cluster["certificate-authority-data"]).decode())
    with tempfile.TemporaryDirectory() as temporary:
        cert, key = (Path(temporary) / name for name in ("cert.pem", "key.pem"))
        for path, field in ((cert, "client-certificate-data"), (key, "client-key-data")):
            path.write_bytes(base64.b64decode(user[field]))
            path.chmod(0o600)
        context.load_cert_chain(str(cert), str(key))
    transport = httpx.HTTPTransport(verify=context, trust_env=False)
    return Client(config=KubeConfig.from_file(KUBE), transport=transport, trust_env=False), transport


@pytest.hookimpl(tryfirst=True)
def pytest_fixture_setup(fixturedef, request):
    if fixturedef.argname != "client":
        return None
    client, transport = authenticated_client()
    request.addfinalizer(transport.close)
    fixturedef.cached_result = (client, fixturedef.cache_key(request), None)
    return client
