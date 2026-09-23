"""The launcher: which transport a launch picks, what it prints, what it refuses.

main() runs for real — argument parsing, the Store, the certificate, the app and
uvicorn.Config.load() — with only Server.run replaced, so nothing listens. What
would have reached Uvicorn is asserted directly, and so is the banner, because
the banner is the only place a host is told which URL to type.
"""

import socket
import ssl

import pytest

pytest.importorskip("gradio")
pytest.importorskip("cryptography")

import webui  # noqa: E402
from videotrim.config.store import Store  # noqa: E402
from videotrim.security import network, tls  # noqa: E402
from videotrim.security.auth import TRANSPORT_WARNING, AuthService  # noqa: E402

LAN = "192.168.1.50"


def free_port():
    with socket.socket() as probe:
        probe.bind(("0.0.0.0", 0))
        return probe.getsockname()[1]


def file_state(path):
    info = path.stat()
    return path.read_bytes(), info.st_mtime_ns, info.st_mode


@pytest.fixture
def quiet(fake_root, monkeypatch):
    """A launcher that reads no CMD_FLAGS.txt and never opens a tunnel or browser."""
    monkeypatch.setattr(webui, "read_cmd_flags", lambda: [])
    monkeypatch.setattr(webui, "start_tunnel", lambda host, port: None)
    monkeypatch.setattr(webui, "open_browser_later", lambda url, delay=1.5: None)
    return fake_root


@pytest.fixture
def launcher(quiet, monkeypatch):
    """A launchable install in the fake root. Returns what Server.run was handed."""
    import uvicorn

    from videotrim.web import server

    root = quiet
    monkeypatch.setattr(server, "ROOT", root)
    monkeypatch.setattr(server, "CACHE", root / "cache")
    monkeypatch.setattr(server, "UPLOAD_DIR", root / "cache" / "uploads")
    monkeypatch.setattr(server, "PROXY_DIR", root / "cache" / "proxies")
    monkeypatch.setattr(server, "GRADIO_TEMP", root / "cache" / "gradio")

    # This machine's addresses, pinned: the route probe and what the hostname
    # resolves to. Tests change `addresses` to move the machine around.
    addresses = [LAN]
    monkeypatch.setattr(network, "primary_address", lambda: addresses[0])
    monkeypatch.setattr(network, "hostname_addresses", lambda family=0: list(addresses))

    AuthService(Store()).create_credentials("hostuser", "a long enough password")

    served = {"addresses": addresses}

    def run(self, sockets=None):
        served["config"] = self.config
        self.started = True

    monkeypatch.setattr(uvicorn.Server, "run", run)
    return served


def launch(*flags):
    return webui.main(["--no-browser", "--listen-port", str(free_port()), *flags])


# --- the default -------------------------------------------------------------
def test_a_normal_launch_is_https_with_the_managed_certificate(launcher, quiet, capsys):
    assert launch() == 0
    config = launcher["config"]
    out = capsys.readouterr().out

    assert isinstance(config.ssl, ssl.SSLContext), "Uvicorn was not given TLS"
    assert config.ssl_keyfile == str(quiet / "data" / "tls" / "videotrim.key")
    assert config.ssl_certfile == str(quiet / "data" / "tls" / "videotrim.crt")
    assert config.ssl_ciphers == tls.CIPHERS

    assert f"https://127.0.0.1:{config.port}" in out
    assert f"https://{LAN}:{config.port}" in out
    assert "http://" not in out
    assert "HTTPS (managed self-signed certificate)" in out
    assert "(new)" in out and "made this launch: none existed yet" in out
    assert "SHA-256" in out and "browser trust" in out
    assert TRANSPORT_WARNING not in out


def test_the_next_launch_reuses_the_certificate(launcher, quiet, capsys):
    launch()
    key = quiet / "data" / "tls" / "videotrim.key"
    before = file_state(key)
    capsys.readouterr()

    assert launch() == 0
    assert "(reused)" in capsys.readouterr().out
    assert file_state(key) == before


def test_moving_networks_renews_and_says_devices_will_be_asked_again(launcher, capsys):
    launch()
    launcher["addresses"][:] = ["10.20.30.40"]
    capsys.readouterr()

    assert launch() == 0
    out = capsys.readouterr().out
    assert "did not cover 10.20.30.40" in out
    assert "will be asked again" in out
    assert "https://10.20.30.40:" in out


def test_an_address_the_certificate_does_not_name_is_never_advertised(launcher, capsys):
    launch()
    launcher["addresses"][:] = [LAN, "172.20.0.1"]  # a virtual adapter appears
    capsys.readouterr()

    assert launch() == 0
    out = capsys.readouterr().out
    assert "(reused)" in out
    assert f"https://{LAN}:" in out
    assert "https://172.20.0.1" not in out
    assert "uncovered" in out and "172.20.0.1" in out


# --- plain HTTP, only when chosen or when nothing leaves the machine ------------
def test_local_only_is_http_on_loopback_without_a_certificate(launcher, quiet, capsys):
    assert launch("--local-only") == 0
    out = capsys.readouterr().out
    assert launcher["config"].ssl is None
    assert launcher["config"].host == "127.0.0.1"
    assert "http://127.0.0.1:" in out
    assert "from elsewhere" not in out
    assert "nothing crosses the network" in out
    assert not (quiet / "data" / "tls").exists()


def test_http_is_plain_and_says_so(launcher, quiet, capsys):
    assert launch("--http") == 0
    out = capsys.readouterr().out
    assert launcher["config"].ssl is None
    assert f"http://{LAN}:" in out
    assert "NOT encrypted" in out
    assert "https://" not in out
    assert not (quiet / "data" / "tls").exists(), "--http never prepares TLS material"


# --- failure never becomes HTTP ------------------------------------------------
def test_a_tls_failure_stops_the_launch_and_never_serves_http(launcher, capsys, monkeypatch):
    def refuse(*args, **kwargs):
        raise PermissionError(13, "Permission denied", "data/tls/videotrim.key")

    monkeypatch.setattr(tls, "_publish", refuse)
    assert launch() == 1
    assert "config" not in launcher, "a listener was started after HTTPS failed"
    err = capsys.readouterr().err
    assert "HTTPS setup failed" in err
    assert "Permission denied" in err
    assert "--http" in err


def test_a_certificate_uvicorn_refuses_stops_the_launch(launcher, capsys, monkeypatch):
    import uvicorn

    def refuse(self):
        raise ssl.SSLError("simulated: key values mismatch")

    monkeypatch.setattr(uvicorn.Config, "load", refuse)
    assert launch() == 1
    assert "config" not in launcher
    assert "HTTPS setup failed" in capsys.readouterr().err


# --- a supplied pair -----------------------------------------------------------
def test_a_supplied_pair_is_served_and_left_alone(launcher, quiet, tmp_path, capsys):
    key_pem, cert_pem = tls._build_pair(("videotrim.home.arpa", "127.0.0.1"), tls._utcnow())
    key, cert = tmp_path / "server.key", tmp_path / "server.pem"
    key.write_bytes(key_pem)
    cert.write_bytes(cert_pem)
    before = file_state(key), file_state(cert)

    assert launch("--tls-keyfile", str(key), "--tls-certfile", str(cert)) == 0
    out = capsys.readouterr().out
    config = launcher["config"]
    assert config.ssl_keyfile == str(key) and config.ssl_certfile == str(cert)
    assert "HTTPS (supplied certificate)" in out
    # The LAN URL the banner prints is not in this certificate, and it says so.
    assert f"does not name {LAN}" in out
    assert (file_state(key), file_state(cert)) == before
    assert not (quiet / "data" / "tls").exists()


# --- flags that cannot work together -----------------------------------------
@pytest.mark.parametrize("flags", [
    ["--http", "--tls-keyfile", "k.pem", "--tls-certfile", "c.pem"],
    ["--http", "--tls-certfile", "c.pem"],
    ["--tls-keyfile", "k.pem"],
    ["--tls-certfile", "c.pem"],
])
def test_contradictory_tls_flags_are_refused_at_parse_time(quiet, flags):
    with pytest.raises(SystemExit) as stopped:
        webui.main(flags)
    assert stopped.value.code == 2
    assert not (quiet / "data").joinpath("app.db").exists()


@pytest.mark.parametrize("flags", [
    ["--share"],
    ["--share", "--listen-host", "192.168.1.5"],
    ["--share", "--tls-keyfile", "k.pem", "--tls-certfile", "c.pem"],
])
def test_share_with_https_is_refused_before_anything_exists(quiet, capsys, flags):
    # The tunnel would print a public link that fails on every request.
    assert webui.main(["--no-browser", *flags]) == 1
    assert "--share cannot reach an HTTPS server" in capsys.readouterr().err
    assert not (quiet / "data" / "app.db").exists(), "refused only after touching data/"
    assert not (quiet / "data" / "tls").exists()


def test_share_with_local_only_serves_http_on_loopback(launcher, capsys):
    assert launch("--share", "--local-only") == 0
    out = capsys.readouterr().out
    assert launcher["config"].ssl is None
    assert launcher["config"].host == "127.0.0.1"
    assert "a --share link is HTTPS" in out


def test_ipv6_urls_are_bracketed():
    assert webui.url_for("https", "::1", 7862) == "https://[::1]:7862"
    assert webui.url_for("https", "[::1]", 7862) == "https://[::1]:7862"
    assert webui.url_for("http", "127.0.0.1", 7862) == "http://127.0.0.1:7862"
