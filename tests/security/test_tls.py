"""HTTPS: the managed certificate's lifecycle, a supplied pair, and the transport.

These exercise videotrim/security/tls.py directly. Nothing here opens a real
listener except the handshake tests at the bottom, which check the cipher policy
against this machine's OpenSSL. The end-to-end HTTPS path lives in
test_live_routes.py and test_https_launch.py.
"""

import datetime
import ipaddress
import os
import socket
import ssl
import threading

import pytest

cryptography = pytest.importorskip("cryptography", reason="cryptography is a hard requirement")

from cryptography import x509  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID  # noqa: E402

from videotrim.security import network, tls  # noqa: E402
from videotrim.security.auth import TRANSPORT_WARNING  # noqa: E402

PRIMARY = "192.168.1.50"
ELSEWHERE = "192.168.7.7"


def managed(host="0.0.0.0", primary=PRIMARY, hostname="Host-1", addresses=None, now=None):
    """ensure_managed_tls with discovery pinned, so tests never read this machine."""
    return tls.ensure_managed_tls(
        host,
        primary=primary,
        hostname=hostname,
        addresses=[primary] if addresses is None else addresses,
        now=now,
    )


def certificate(material):
    return x509.load_pem_x509_certificate(material.cert_path.read_bytes())


def san(cert):
    names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    return (set(names.get_values_for_type(x509.DNSName))
            | {str(ip) for ip in names.get_values_for_type(x509.IPAddress)})


def file_state(path):
    info = path.stat()
    return path.read_bytes(), info.st_mtime_ns, info.st_mode


def write_pair(key_path, cert_path, names=("localhost",), key_size=2048, days=365,
               start=None, with_san=True, ca=False, issuer=None, password=None):
    """A key and certificate made outside tls.py, for supplied-pair and damage tests."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=key_size)
    start = start or datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0] if names else "x")])
    issuer_name, signing_key = (issuer or (subject, key))
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer_name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start)
        .not_valid_after(start + datetime.timedelta(days=days))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
    )
    if with_san and names:
        general = []
        for name in names:
            try:
                general.append(x509.IPAddress(ipaddress.ip_address(name)))
            except ValueError:
                general.append(x509.DNSName(name))
        builder = builder.add_extension(x509.SubjectAlternativeName(general), critical=False)
    cert = builder.sign(signing_key, hashes.SHA256())
    encryption = (serialization.BestAvailableEncryption(password) if password
                  else serialization.NoEncryption())
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                           serialization.PrivateFormat.PKCS8, encryption))
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return key, cert


# --- the certificate profile ---------------------------------------------------
def test_first_launch_creates_a_pair_matching_the_profile(fake_root):
    material = managed()
    cert = certificate(material)
    key = serialization.load_pem_private_key(material.key_path.read_bytes(), password=None)

    assert material.managed and material.regenerated
    assert material.reason == tls.FIRST_CERTIFICATE
    assert material.key_path == fake_root / "data" / "tls" / "videotrim.key"
    assert material.cert_path == fake_root / "data" / "tls" / "videotrim.crt"

    assert isinstance(key, rsa.RSAPrivateKey) and key.key_size >= 2048
    assert cert.signature_hash_algorithm.name == "sha256"
    assert cert.public_key().public_numbers() == key.public_key().public_numbers()

    constraints = cert.extensions.get_extension_for_class(x509.BasicConstraints)
    assert constraints.critical and constraints.value.ca is False
    usage = cert.extensions.get_extension_for_class(x509.KeyUsage)
    assert usage.critical and usage.value.digital_signature and usage.value.key_encipherment
    assert not usage.value.key_cert_sign and not usage.value.crl_sign
    eku = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert ExtendedKeyUsageOID.SERVER_AUTH in eku
    cert.extensions.get_extension_for_class(x509.SubjectKeyIdentifier)

    assert {"localhost", "127.0.0.1", "::1", PRIMARY} <= san(cert)
    # Apple refuses TLS server certificates valid for longer than 825 days.
    lifetime = cert.not_valid_after_utc - cert.not_valid_before_utc
    assert lifetime <= datetime.timedelta(days=825)
    assert cert.not_valid_before_utc < datetime.datetime.now(datetime.timezone.utc)


def test_the_pair_loads_the_way_uvicorn_loads_it(fake_root):
    material = managed()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(material.cert_path), str(material.key_path))


def test_every_new_certificate_gets_a_fresh_random_serial(fake_root):
    # Firefox refuses outright a certificate whose issuer and serial it has
    # already seen. A replacement for the same machine has the same issuer, so
    # only the serial keeps the two apart.
    material = managed()
    first = certificate(material)
    material.cert_path.unlink()
    second = certificate(managed())
    assert first.issuer == second.issuer
    assert first.serial_number != second.serial_number
    assert 0 < second.serial_number < 2 ** 159


def test_the_fingerprint_is_the_certificates_sha256(fake_root):
    material = managed()
    expected = certificate(material).fingerprint(hashes.SHA256()).hex().upper()
    assert material.fingerprint.replace(":", "") == expected
    assert len(material.fingerprint) == 95


# --- identities ----------------------------------------------------------------
@pytest.mark.parametrize("bind", ["0.0.0.0", "::", "[::]", "", "*"])
def test_a_wildcard_bind_is_never_an_identity(fake_root, bind):
    assert tls.required_identities(bind, primary="") == tls.LOOPBACK_IDENTITIES
    names = san(certificate(managed(host=bind, primary="", addresses=[])))
    assert not names & {"0.0.0.0", "::", "*", ""}


def test_an_explicit_dns_listen_host_is_named(fake_root):
    assert "videotrim.home.arpa" in tls.required_identities("VideoTrim.Home.Arpa.")
    material = managed(host="videotrim.home.arpa")
    assert "videotrim.home.arpa" in san(certificate(material))
    assert material.covers("VIDEOTRIM.home.arpa")


@pytest.mark.parametrize("explicit", ["192.168.1.5", "169.254.10.10", "[fd00::5]"])
def test_an_explicit_ip_listen_host_is_named_even_if_unusual(fake_root, explicit):
    # Explicit values are intentional: a link-local address a discovery filter
    # would drop still goes in when the host asked to bind it.
    material = managed(host=explicit)
    assert tls.canonical_identity(explicit) in san(certificate(material))


def test_an_explicit_host_no_certificate_can_hold_is_refused(fake_root):
    with pytest.raises(tls.TLSError, match="cannot be written into a certificate"):
        managed(host="bad_host!")


def test_the_primary_lan_address_is_required_when_serving_everywhere():
    assert PRIMARY in tls.required_identities("0.0.0.0", primary=PRIMARY)
    assert PRIMARY not in tls.required_identities("127.0.0.1", primary=PRIMARY)


def test_identities_are_deduplicated(fake_root):
    material = managed(hostname="host-1", addresses=[PRIMARY, PRIMARY, "::ffff:192.168.1.50"])
    assert len(material.identities) == len(set(material.identities))
    assert material.identities.count(PRIMARY) == 1


def test_malformed_and_unusable_discovered_values_are_ignored(fake_root):
    junk = ["not-an-ip", "", "224.0.0.1", "169.254.1.1", "0.0.0.0", "127.0.1.1",
            "fe80::1%eth0", "240.0.0.1"]
    material = managed(hostname="bad_name", addresses=[PRIMARY] + junk)
    names = san(certificate(material))
    assert names == {"localhost", "127.0.0.1", "::1", PRIMARY}


def test_a_non_ascii_hostname_is_encoded_or_skipped_never_fatal(fake_root):
    material = managed(hostname="Straße-PC")
    assert all(name.isascii() for name in san(certificate(material)))


# --- reuse ---------------------------------------------------------------------
def test_a_healthy_pair_is_reused_untouched(fake_root):
    first = managed()
    before = file_state(first.key_path), file_state(first.cert_path)
    second = managed()
    assert not second.regenerated and second.reason == ""
    assert (file_state(second.key_path), file_state(second.cert_path)) == before
    assert second.fingerprint == first.fingerprint


def test_extra_names_do_not_force_a_new_certificate(fake_root):
    first = managed(addresses=[PRIMARY, "10.0.0.9", "172.20.0.1"])
    second = managed(addresses=[PRIMARY])
    assert not second.regenerated
    assert second.fingerprint == first.fingerprint


def test_a_new_secondary_address_does_not_force_a_new_certificate(fake_root):
    # The churn this design exists to avoid: virtual adapters (WSL, Hyper-V,
    # VPNs) that change address every boot must not cost every device a warning.
    first = managed(addresses=[PRIMARY])
    second = managed(addresses=[PRIMARY, "172.31.9.9"])
    assert not second.regenerated
    assert second.fingerprint == first.fingerprint
    assert not second.covers("172.31.9.9")


# --- regeneration --------------------------------------------------------------
def _other_key(path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                       serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()))


DAMAGE = {
    "missing key": (lambda key, cert: key.unlink(), "videotrim.key was missing"),
    "missing certificate": (lambda key, cert: cert.unlink(), "videotrim.crt was missing"),
    "malformed key": (lambda key, cert: key.write_bytes(b"not a key"), "could not be read"),
    "malformed certificate": (lambda key, cert: cert.write_bytes(b"-----BEGIN CERT"),
                              "could not be read"),
    "empty certificate": (lambda key, cert: cert.write_bytes(b""), "could not be read"),
    "mismatched pair": (lambda key, cert: _other_key(key), "did not match"),
}


@pytest.mark.parametrize("damage", sorted(DAMAGE))
def test_a_damaged_pair_is_replaced_before_it_reaches_the_server(fake_root, damage):
    first = managed()
    harm, reason = DAMAGE[damage]
    harm(first.key_path, first.cert_path)

    second = managed()
    assert second.regenerated and reason in second.reason
    ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(
        str(second.cert_path), str(second.key_path))


def test_an_expired_certificate_is_replaced(fake_root):
    first = managed()
    later = certificate(first).not_valid_after_utc + datetime.timedelta(days=1)
    second = managed(now=later)
    assert second.regenerated and "expired" in second.reason


def test_a_certificate_close_to_expiry_is_replaced(fake_root):
    # The server reads its certificate once; a launch just before expiry would
    # otherwise serve an expired certificate for its whole run.
    first = managed()
    soon = certificate(first).not_valid_after_utc - datetime.timedelta(days=10)
    second = managed(now=soon)
    assert second.regenerated and "expires on" in second.reason


def test_a_certificate_from_the_future_is_replaced(fake_root):
    first = managed()
    earlier = certificate(first).not_valid_before_utc - datetime.timedelta(days=2)
    second = managed(now=earlier)
    assert second.regenerated and "only starts on" in second.reason


def test_a_certificate_without_names_is_replaced(fake_root):
    first = managed()
    write_pair(first.key_path, first.cert_path, with_san=False)
    second = managed()
    assert second.regenerated and "named no addresses" in second.reason


def test_a_weak_key_is_replaced(fake_root):
    first = managed()
    write_pair(first.key_path, first.cert_path, names=("localhost", "127.0.0.1", "::1", PRIMARY),
               key_size=1024)
    second = managed()
    assert second.regenerated and "weaker" in second.reason


def test_a_new_primary_address_is_covered_and_old_names_carry_over(fake_root):
    home = managed(primary=PRIMARY)
    office = managed(primary=ELSEWHERE)
    assert office.regenerated and ELSEWHERE in office.reason
    assert office.covers(ELSEWHERE) and office.covers(PRIMARY)

    # Back home: the carried-over name means no new certificate, and no new
    # warning on every device.
    back = managed(primary=PRIMARY)
    assert not back.regenerated
    assert back.fingerprint == office.fingerprint != home.fingerprint


def test_carried_over_names_are_capped(fake_root):
    first = managed()
    many = ["localhost", "127.0.0.1", "::1"] + [f"10.1.{n // 250}.{n % 250 + 1}" for n in range(60)]
    write_pair(first.key_path, first.cert_path, names=many, days=1)  # expires soon
    second = managed(primary=ELSEWHERE)
    names = san(certificate(second))
    assert len(names) <= tls.MAX_IDENTITIES
    assert {"localhost", "127.0.0.1", "::1", ELSEWHERE} <= names


# --- atomicity -----------------------------------------------------------------
def test_a_generation_failure_leaves_the_working_pair_alone(fake_root, monkeypatch):
    first = managed(primary=PRIMARY)
    before = file_state(first.key_path), file_state(first.cert_path)

    def broken(names, now):
        raise ValueError("simulated failure")

    monkeypatch.setattr(tls, "_build_pair", broken)
    with pytest.raises(tls.TLSError, match="simulated failure"):
        managed(primary=ELSEWHERE)
    assert (file_state(first.key_path), file_state(first.cert_path)) == before


def test_a_half_replaced_pair_is_refused_then_healed(fake_root, monkeypatch):
    first = managed(primary=PRIMARY)
    real_replace = tls._replace

    def key_then_fail(source, target):
        if target.name == tls.CERT_NAME:
            raise PermissionError(13, "Permission denied", str(target))
        real_replace(source, target)

    monkeypatch.setattr(tls, "_replace", key_then_fail)
    with pytest.raises(tls.TLSError, match="Permission denied"):
        managed(primary=ELSEWHERE)
    monkeypatch.setattr(tls, "_replace", real_replace)

    # The key moved and the certificate did not: the next launch sees the
    # mismatch and replaces the pair rather than serving it.
    healed = managed(primary=ELSEWHERE)
    assert healed.regenerated and "did not match" in healed.reason
    assert healed.fingerprint != first.fingerprint
    assert not list(healed.cert_path.parent.glob(".videotrim-*"))


def test_no_temporary_files_remain_after_success(fake_root):
    material = managed()
    assert sorted(p.name for p in material.cert_path.parent.iterdir()) == [
        ".lock", "videotrim.crt", "videotrim.key"]


def test_files_staged_by_a_crashed_launch_are_swept(fake_root):
    material = managed()
    leftover = material.cert_path.parent / ".videotrim-crashed.tmp"
    leftover.write_bytes(b"half a key")
    managed()
    assert not leftover.exists()


def test_concurrent_launches_never_interleave_a_pair(fake_root):
    primaries = ["192.168.10.1", "192.168.20.1", "192.168.30.1", "192.168.40.1"]
    barrier = threading.Barrier(len(primaries))
    errors = []

    def launch(address):
        try:
            barrier.wait()
            managed(primary=address)
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=launch, args=(a,)) for a in primaries]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not errors

    final = managed(primary=primaries[0])
    assert not final.regenerated
    assert all(final.covers(address) for address in primaries)
    ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(
        str(final.cert_path), str(final.key_path))


# --- permissions ---------------------------------------------------------------
posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")


@posix_only
def test_the_tls_directory_and_key_are_owner_only(fake_root):
    material = managed()
    assert material.key_path.parent.stat().st_mode & 0o777 == 0o700
    assert material.key_path.stat().st_mode & 0o777 == 0o600
    assert material.cert_path.stat().st_mode & 0o777 == 0o644


@posix_only
def test_key_permissions_are_reapplied_when_reused(fake_root):
    material = managed()
    os.chmod(material.key_path, 0o644)
    again = managed()
    assert not again.regenerated
    assert again.key_path.stat().st_mode & 0o777 == 0o600


# --- a supplied pair -------------------------------------------------------------
@pytest.fixture
def supplied(tmp_path):
    folder = tmp_path / "supplied"
    folder.mkdir()
    key_path, cert_path = folder / "server.key", folder / "server.pem"
    write_pair(key_path, cert_path, names=("videotrim.home.arpa", "192.168.1.50"))
    return key_path, cert_path


def test_a_supplied_pair_is_used_and_never_modified(fake_root, supplied):
    key_path, cert_path = supplied
    if os.name == "posix":
        os.chmod(key_path, 0o640)  # not what a managed key gets; must stay as it is
    before = file_state(key_path), file_state(cert_path)

    transport = tls.resolve_transport("0.0.0.0", keyfile=str(key_path), certfile=str(cert_path))
    assert transport.scheme == "https" and transport.mode == tls.SUPPLIED
    assert transport.uvicorn_options()["ssl_keyfile"] == str(key_path)
    assert transport.uvicorn_options()["ssl_certfile"] == str(cert_path)
    assert transport.material.covers("videotrim.home.arpa")
    assert transport.material.covers("192.168.1.50")
    assert not transport.material.covers("192.168.1.51")
    assert not transport.material.warnings

    assert (file_state(key_path), file_state(cert_path)) == before
    assert not (fake_root / "data" / "tls").exists(), "a supplied pair never touches data/tls"


def test_an_encrypted_supplied_key_is_refused_without_prompting(tmp_path):
    # OpenSSL would otherwise ask for the passphrase on the console and wait.
    key_path, cert_path = tmp_path / "enc.key", tmp_path / "enc.pem"
    write_pair(key_path, cert_path, password=b"correct horse")
    with pytest.raises(tls.TLSError, match="passphrase"):
        tls.validate_user_pair(key_path, cert_path)


def test_a_mismatched_supplied_pair_is_refused(tmp_path, supplied):
    key_path, _ = supplied
    other_key, other_cert = tmp_path / "other.key", tmp_path / "other.pem"
    write_pair(other_key, other_cert)
    with pytest.raises(tls.TLSError, match="does not belong"):
        tls.validate_user_pair(key_path, other_cert)


def test_malformed_supplied_files_are_named(tmp_path, supplied):
    key_path, cert_path = supplied
    junk = tmp_path / "junk.pem"
    junk.write_text("hello", encoding="utf-8")
    with pytest.raises(tls.TLSError, match="junk.pem is not a PEM certificate"):
        tls.validate_user_pair(key_path, junk)
    with pytest.raises(tls.TLSError, match="junk.pem is not a PEM private key"):
        tls.validate_user_pair(junk, cert_path)


def test_missing_supplied_files_are_named(tmp_path, supplied):
    key_path, _ = supplied
    with pytest.raises(tls.TLSError, match="--tls-certfile .*nope.pem: no such file"):
        tls.validate_user_pair(key_path, tmp_path / "nope.pem")
    with pytest.raises(tls.TLSError, match="is not a file"):
        tls.validate_user_pair(key_path, tmp_path)


def test_one_supplied_file_without_the_other_is_refused(supplied):
    key_path, cert_path = supplied
    with pytest.raises(tls.TLSError, match="together"):
        tls.resolve_transport("0.0.0.0", keyfile=str(key_path))
    with pytest.raises(tls.TLSError, match="together"):
        tls.resolve_transport("0.0.0.0", certfile=str(cert_path))


def test_an_expired_supplied_certificate_warns_but_is_the_hosts_call(tmp_path):
    key_path, cert_path = tmp_path / "old.key", tmp_path / "old.pem"
    long_ago = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=400)
    write_pair(key_path, cert_path, start=long_ago, days=30)
    material = tls.validate_user_pair(key_path, cert_path)
    assert any("expired" in warning for warning in material.warnings)


def test_a_wildcard_supplied_certificate_covers_one_label(tmp_path):
    key_path, cert_path = tmp_path / "wild.key", tmp_path / "wild.pem"
    write_pair(key_path, cert_path, names=("*.home.arpa",))
    material = tls.validate_user_pair(key_path, cert_path)
    assert material.covers("videotrim.home.arpa")
    assert not material.covers("a.b.home.arpa")
    assert not material.covers("home.arpa")


def test_a_supplied_chain_is_accepted_and_describes_its_leaf(tmp_path):
    ca_key_path, ca_path = tmp_path / "ca.key", tmp_path / "ca.pem"
    ca_key, ca_cert = write_pair(ca_key_path, ca_path, names=("Test CA",), ca=True,
                                 with_san=False)
    key_path, leaf_path = tmp_path / "leaf.key", tmp_path / "leaf.pem"
    _, leaf = write_pair(key_path, leaf_path, names=("videotrim.home.arpa",),
                         issuer=(ca_cert.subject, ca_key))
    chain = tmp_path / "chain.pem"
    chain.write_bytes(leaf_path.read_bytes() + ca_path.read_bytes())

    material = tls.validate_user_pair(key_path, chain)
    assert material.covers("videotrim.home.arpa")
    assert material.fingerprint.replace(":", "") == leaf.fingerprint(hashes.SHA256()).hex().upper()


# --- choosing a transport --------------------------------------------------------
@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "[::1]", "127.0.0.2"])
def test_a_loopback_bind_serves_http_and_makes_no_certificate(fake_root, host):
    transport = tls.resolve_transport(host)
    assert transport.scheme == "http" and transport.mode == tls.LOOPBACK
    assert transport.uvicorn_options() == {}
    assert not (fake_root / "data" / "tls").exists()


def test_http_is_only_ever_an_explicit_choice(fake_root):
    transport = tls.resolve_transport("0.0.0.0", plaintext=True)
    assert transport.scheme == "http" and transport.mode == tls.PLAINTEXT
    assert not (fake_root / "data" / "tls").exists(), "--http never prepares TLS material"


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.5", "videotrim.home.arpa"])
def test_serving_the_network_is_managed_https(fake_root, host):
    transport = tls.resolve_transport(host, primary=PRIMARY, hostname="h", addresses=[])
    assert transport.scheme == "https" and transport.mode == tls.MANAGED
    options = transport.uvicorn_options()
    assert options["ssl_keyfile"].endswith("videotrim.key")
    assert options["ssl_certfile"].endswith("videotrim.crt")
    assert options["ssl_ciphers"] == tls.CIPHERS


def test_a_failure_is_raised_never_downgraded(fake_root, monkeypatch):
    def refuse(*args, **kwargs):
        raise PermissionError(13, "Permission denied", "data/tls/videotrim.key")

    monkeypatch.setattr(tls, "_publish", refuse)
    with pytest.raises(tls.TLSError, match="Permission denied"):
        tls.resolve_transport("0.0.0.0", primary=PRIMARY, hostname="h", addresses=[])


def test_expects_tls_matches_what_resolve_transport_does():
    assert tls.expects_tls("0.0.0.0")
    assert tls.expects_tls("192.168.1.5")
    assert not tls.expects_tls("127.0.0.1")
    assert not tls.expects_tls("0.0.0.0", plaintext=True)
    assert tls.expects_tls("127.0.0.1", supplied=True)


def test_missing_cryptography_is_an_explicit_failure(fake_root, monkeypatch):
    monkeypatch.setattr(tls, "x509", None)
    with pytest.raises(tls.TLSError, match="one_click.py --update"):
        tls.resolve_transport("0.0.0.0", primary=PRIMARY, hostname="h", addresses=[])


# --- what the pages say ----------------------------------------------------------
def test_transport_notes_describe_the_connection_truthfully():
    assert tls.transport_note("https", tls.MANAGED) == tls.MANAGED_HTTPS_NOTE
    assert tls.transport_note("https", tls.SUPPLIED) == tls.HTTPS_NOTE
    assert tls.transport_note("https", None) == tls.HTTPS_NOTE
    assert tls.transport_note("http", tls.LOOPBACK) == tls.LOOPBACK_NOTE
    # A tunnel carries loopback traffic off the machine, so the local note is wrong there.
    assert tls.transport_note("http", tls.LOOPBACK, tunnel_active=True) == TRANSPORT_WARNING
    assert tls.transport_note("http", tls.PLAINTEXT) == TRANSPORT_WARNING
    assert tls.transport_note("http", None) == TRANSPORT_WARNING
    # A self-signed certificate is never described as trusted or verified.
    for note in (tls.MANAGED_HTTPS_NOTE, tls.HTTPS_NOTE):
        for claim in ("verified", "fully trusted", "no warning"):
            assert claim not in note.lower()


# --- this machine's addresses ----------------------------------------------------
def test_own_addresses_unions_the_hostname_and_the_route_probe(monkeypatch):
    monkeypatch.setattr(network, "hostname_addresses", lambda family=0: ["10.0.0.2", "fe80::1"])
    monkeypatch.setattr(network, "primary_address", lambda: "192.168.1.9")
    assert network.own_addresses() == frozenset({"10.0.0.2", "fe80::1", "192.168.1.9"})
    monkeypatch.setattr(network, "primary_address", lambda: "")
    assert "" not in network.own_addresses()


# --- the cipher policy, against this machine's OpenSSL ---------------------------
def _handshake(material, client_context):
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(str(material.cert_path), str(material.key_path))
    server_context.set_ciphers(tls.CIPHERS)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def serve():
        connection, _ = listener.accept()
        try:
            server_context.wrap_socket(connection, server_side=True).close()
        except (ssl.SSLError, OSError):
            connection.close()

    thread = threading.Thread(target=serve)
    thread.start()
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=10) as raw:
            with client_context.wrap_socket(raw, server_hostname="127.0.0.1") as wrapped:
                return wrapped.version(), wrapped.cipher()[0]
    finally:
        thread.join(timeout=10)
        listener.close()


def test_a_client_trusting_only_the_generated_certificate_verifies_it(fake_root):
    material = managed()
    client = ssl.create_default_context(cafile=str(material.cert_path))
    version, _ = _handshake(material, client)
    assert version in ("TLSv1.2", "TLSv1.3")


def test_legacy_cbc_sha1_suites_are_refused(fake_root):
    material = managed()
    client = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    client.check_hostname = False
    client.verify_mode = ssl.CERT_NONE
    client.maximum_version = ssl.TLSVersion.TLSv1_2
    client.set_ciphers("ECDHE-RSA-AES128-SHA")
    with pytest.raises((ssl.SSLError, OSError)):
        _handshake(material, client)

    modern = ssl.create_default_context(cafile=str(material.cert_path))
    modern.maximum_version = ssl.TLSVersion.TLSv1_2
    version, cipher = _handshake(material, modern)
    assert version == "TLSv1.2" and ("GCM" in cipher or "CHACHA20" in cipher)
