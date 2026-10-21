"""Verify TLS stays enabled and only the janitor client fixture is replaced."""

import base64
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import kind_slow_janitor as plugin
import pytest
import yaml


def test_client_loads_ca_and_client_certificate_without_leaving_key_files(tmp_path, monkeypatch):
    path = tmp_path / "kubeconfig"
    monkeypatch.setattr(plugin, "KUBE", path)
    monkeypatch.setenv("OSDC_KIND_SLOW_KUBECONFIG", str(path))

    def encode(value):
        return base64.b64encode(value.encode()).decode()

    config = {
        "current-context": plugin.CONTEXT,
        "clusters": [{"name": plugin.CONTEXT, "cluster": {"certificate-authority-data": encode("ca")}}],
        "users": [
            {
                "name": plugin.CONTEXT,
                "user": {"client-certificate-data": encode("cert"), "client-key-data": encode("key")},
            }
        ],
    }
    path.write_text(yaml.safe_dump(config))
    context = Mock()
    cert_paths = []

    def load(cert, key):
        cert_paths.extend([Path(cert), Path(key)])
        assert Path(cert).read_text() == "cert"
        assert Path(key).read_text() == "key"
        assert all(p.stat().st_mode & 0o777 == 0o600 for p in cert_paths)

    context.load_cert_chain.side_effect = load
    factory = Mock(return_value=context)
    monkeypatch.setattr(plugin.ssl, "create_default_context", factory)
    transport = Mock()
    transport_factory = Mock(return_value=transport)
    monkeypatch.setattr(plugin.httpx, "HTTPTransport", transport_factory)
    monkeypatch.setattr(plugin.KubeConfig, "from_file", Mock(return_value="config"))
    client_factory = Mock(return_value="authenticated")
    monkeypatch.setattr(plugin, "Client", client_factory)
    assert plugin.authenticated_client() == ("authenticated", transport)
    factory.assert_called_once_with(cadata="ca")
    transport_factory.assert_called_once_with(verify=context, trust_env=False)
    assert all(not p.exists() for p in cert_paths)
    monkeypatch.delenv("OSDC_KIND_SLOW_KUBECONFIG")
    with pytest.raises(ValueError, match="isolated"):
        plugin.authenticated_client()
    monkeypatch.setenv("OSDC_KIND_SLOW_KUBECONFIG", str(path))
    config["current-context"] = "other"
    path.write_text(yaml.safe_dump(config))
    with pytest.raises(ValueError, match="Unexpected"):
        plugin.authenticated_client()


def test_hook_overrides_only_client_fixture_and_closes_transport(monkeypatch):
    transport = Mock()
    monkeypatch.setattr(plugin, "authenticated_client", Mock(return_value=("client", transport)))
    fixture = SimpleNamespace(argname="other", cache_key=lambda request: "key")
    request = Mock()
    assert plugin.pytest_fixture_setup(fixture, request) is None
    fixture.argname = "client"
    assert plugin.pytest_fixture_setup(fixture, request) == "client"
    assert fixture.cached_result == ("client", "key", None)
    request.addfinalizer.assert_called_once_with(transport.close)
