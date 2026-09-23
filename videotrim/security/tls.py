"""HTTPS for the web UI: a certificate Video Trim manages, or the host's own.

Serving the whole network is the default, and over plain HTTP the session
cookie crosses that network in the clear. This module is what lets a normal
launch be HTTPS instead, with nothing for the host to set up:

    managed     a self-signed key and certificate in data/tls/, made on the
                first launch and reused for as long as they stay sound.
    supplied    --tls-keyfile / --tls-certfile. Read and checked, and otherwise
                left exactly as they are: never rewritten, renamed or chmodded.

What a self-signed certificate buys, stated plainly: the connection is
encrypted, but no public authority vouches for the server, so each device's
browser warns the first time it connects. Nothing here touches an operating
system or browser trust store to make that warning go away.

Stability is the point of the managed pair. A new certificate brings the warning
back on every device that accepted the old one, so it is replaced only when it
is broken, expired or about to be, or no longer names an address this launch
prints. Addresses that come and go — virtual adapters, VPNs — are written into a
new certificate but are never on their own a reason to make one, and every name
an old certificate covered carries over into its replacement.
"""

import contextlib
import datetime
import ipaddress
import os
import re
import socket
import ssl
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from . import fs_boundary, network
from .auth import TRANSPORT_WARNING

try:
    from cryptography import x509
    from cryptography.exceptions import UnsupportedAlgorithm
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
except Exception:  # pragma: no cover - depends on the venv
    x509 = UnsupportedAlgorithm = hashes = serialization = rsa = None
    ExtendedKeyUsageOID = NameOID = None

TLS_DIRNAME = "tls"
KEY_NAME = "videotrim.key"
CERT_NAME = "videotrim.crt"
LOCK_NAME = ".lock"
_TEMP_PREFIX = ".videotrim-"
_TEMP_SUFFIX = ".tmp"

RSA_KEY_SIZE = 2048
VALIDITY = datetime.timedelta(days=365)
# Backdated a day, so a device whose clock runs a little slow is not handed a
# certificate that starts in its future.
BACKDATE = datetime.timedelta(days=1)
# The server reads its certificate once, at start. Replacing it this close to
# expiry keeps a server launched the day before from serving an expired one.
RENEW_BEFORE = datetime.timedelta(days=30)
# Names carried over from earlier certificates are capped, so a laptop that has
# been on many networks does not grow an ever longer certificate.
MAX_IDENTITIES = 32

# Forward-secret AEAD suites only, for TLS 1.2 clients; TLS 1.3 is unaffected.
# Uvicorn before 0.48 defaults to "TLSv1", which replaces Python's hardened list
# and brings back CBC-SHA1 suites, so the list is always passed explicitly.
CIPHERS = "ECDHE+AESGCM:ECDHE+CHACHA20"

# How a launch serves.
MANAGED = "managed"      # HTTPS with Video Trim's own certificate
SUPPLIED = "supplied"    # HTTPS with the host's --tls-keyfile/--tls-certfile
PLAINTEXT = "http"       # HTTP, because the host passed --http
LOOPBACK = "loopback"    # HTTP, listening on this machine only

# Where a server listens, never what a client connects to — so never a name in
# a certificate.
WILDCARD_BINDS = frozenset({"", "*", "0.0.0.0", "::", "[::]"})
LOOPBACK_IDENTITIES = ("localhost", "127.0.0.1", "::1")

# The reason given when a launch finds no managed pair at all.
FIRST_CERTIFICATE = "none existed yet"

_LOCK_TIMEOUT_SECONDS = 20.0
_REPLACE_ATTEMPTS = 10
_DNS_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")

CRYPTOGRAPHY_MISSING = (
    "the 'cryptography' package is not installed in the venv, so no certificate "
    "can be made. Reinstall the dependencies and start again:\n\n"
    "    python one_click.py --update"
)

# What the pages say about the connection they arrived on. The certificate
# wording follows the design intent: encrypted, and honest that the browser's
# warning is expected rather than claiming the certificate is trusted.
MANAGED_HTTPS_NOTE = (
    "This connection is encrypted. Video Trim generated its own certificate for "
    "it, and browsers do not automatically trust locally generated certificates, "
    "so this device may have shown a warning the first time. Video Trim does not "
    "modify your device's trust store."
)
HTTPS_NOTE = "This connection is encrypted with HTTPS."
LOOPBACK_NOTE = (
    "This connection stays on this machine: Video Trim is only listening here, "
    "so nothing is sent over the network."
)


class TLSError(RuntimeError):
    """HTTPS could not be set up. The message is written for the host's console."""


class _PasswordRequested(Exception):
    """OpenSSL asked for a key's passphrase. See _refuse_password."""


@dataclass(frozen=True)
class TLSMaterial:
    """A key/certificate pair that has been checked and may be given to Uvicorn."""

    key_path: Path
    cert_path: Path
    managed: bool
    regenerated: bool = False
    identities: tuple = ()
    reason: str = ""  # why a managed pair was made this launch; "" when reused
    fingerprint: str = ""
    warnings: tuple = ()

    def covers(self, name):
        """True when this certificate names ``name``, as a browser would check."""
        wanted = canonical_identity(name)
        return bool(wanted) and any(_matches(pattern, wanted) for pattern in self.identities)


@dataclass(frozen=True)
class Transport:
    """How this launch serves. The launcher, the banner and the pages share it."""

    scheme: str
    mode: str
    material: object = None

    @property
    def tls(self):
        return self.scheme == "https"

    def uvicorn_options(self):
        """The keyword arguments Uvicorn needs for this transport."""
        if not self.tls:
            return {}
        return {
            "ssl_keyfile": str(self.material.key_path),
            "ssl_certfile": str(self.material.cert_path),
            "ssl_ciphers": CIPHERS,
        }


# --- choosing a transport ------------------------------------------------------
def expects_tls(host, plaintext=False, supplied=False):
    """Whether a launch with these options serves HTTPS.

    Known before anything is generated, so an option HTTPS cannot serve — a
    --share tunnel — is refused before any file or listener exists.
    """
    if plaintext:
        return False
    return bool(supplied) or not is_loopback_bind(host)


def resolve_transport(host, plaintext=False, keyfile=None, certfile=None,
                      **discovery):
    """Decide how this launch serves, preparing its certificate. Raises TLSError.

    A listener bound to loopback serves HTTP: its traffic never leaves this
    machine, and browsers already treat http://127.0.0.1 as a secure context,
    so HTTPS there would add a certificate warning and no protection.
    """
    if plaintext:
        return Transport("http", PLAINTEXT)
    if keyfile or certfile:
        if not (keyfile and certfile):
            raise TLSError("--tls-keyfile and --tls-certfile have to be given together.")
        return Transport("https", SUPPLIED, validate_user_pair(keyfile, certfile))
    if is_loopback_bind(host):
        return Transport("http", LOOPBACK)
    return Transport("https", MANAGED, ensure_managed_tls(host, **discovery))


def transport_note(scheme, mode=None, tunnel_active=False):
    """What a page says about the connection it was requested over."""
    if scheme == "https":
        return MANAGED_HTTPS_NOTE if mode == MANAGED else HTTPS_NOTE
    if mode == LOOPBACK and not tunnel_active:
        return LOOPBACK_NOTE
    return TRANSPORT_WARNING


# --- names ---------------------------------------------------------------------
def is_wildcard(host):
    return str(host or "").strip() in WILDCARD_BINDS


def is_loopback_bind(host):
    """True when a listener on ``host`` can only be reached from this machine."""
    text = _unbracket(str(host or "").strip()).lower()
    if text == "localhost":
        return True
    address = _ip(text)
    return address is not None and address.is_loopback


def canonical_identity(value):
    """One spelling per name: a compressed IP address, or a lower-case A-label."""
    text = _unbracket(str(value or "").strip())
    address = _ip(text)
    if address is not None:
        return str(address)
    return _dns_name(text)


def nameable(address):
    """True when a discovered address is one a certificate would name."""
    return bool(_discovered_ip(address))


def required_identities(host, primary=None):
    """What the certificate must name for this launch. A gap means a new one.

    Loopback always, the explicit --listen-host, and — when serving every
    interface — the LAN address the banner leads with. Nothing else, because
    every new certificate costs every device another warning.
    """
    names = list(LOOPBACK_IDENTITIES)
    if is_wildcard(host):
        names.append(_discovered_ip(network.primary_address() if primary is None else primary))
    else:
        # Explicit values are intentional, so they are never filtered the way
        # discovered addresses are — only refused when no certificate can hold them.
        explicit = canonical_identity(host)
        if not explicit:
            raise TLSError(
                f"--listen-host {host} cannot be written into a certificate. Use an "
                "IP address or a plain DNS name."
            )
        names.append(explicit)
    return _unique(names)


def extra_identities(host, hostname=None, addresses=None):
    """Names worth writing into a new certificate, but never a reason to make one."""
    if not is_wildcard(host):
        return ()
    names = [_dns_name(_hostname() if hostname is None else hostname)]
    found = network.hostname_addresses(socket.AF_INET) if addresses is None else addresses
    names.extend(_discovered_ip(address) for address in found)
    return _unique(names)


# --- the managed pair ------------------------------------------------------------
def managed_paths():
    """(directory, key, certificate) for the managed pair, inside data/."""
    directory = Path(fs_boundary.DATA_DIR) / TLS_DIRNAME
    return directory, directory / KEY_NAME, directory / CERT_NAME


def ensure_managed_tls(host, primary=None, hostname=None, addresses=None, now=None):
    """The managed pair for this launch: reused when sound, replaced when not."""
    if x509 is None:
        raise TLSError(CRYPTOGRAPHY_MISSING)
    required = required_identities(host, primary)
    extras = extra_identities(host, hostname, addresses)
    now = now or _utcnow()
    directory, key_path, cert_path = managed_paths()
    try:
        with certificate_lock():
            _sweep_temporaries(directory)
            reason, previous = _inspect(key_path, cert_path, required, now)
            if reason:
                names = _unique(required + extras + previous)[:MAX_IDENTITIES]
                try:
                    pair = _build_pair(names, now)
                except Exception as exc:
                    raise TLSError(f"a new certificate could not be generated: {exc}") from exc
                _publish(directory, pair, key_path, cert_path)
                # Read back exactly as the next launch would, so a pair that
                # fails its own check never reaches the listener.
                problem, _ = _inspect(key_path, cert_path, required, now)
                if problem:
                    raise TLSError(f"the new certificate failed its own check: {problem}.")
            warnings = _restrict_key(key_path)
            _openssl_load(key_path, cert_path)
            certificate = x509.load_pem_x509_certificate(cert_path.read_bytes())
    except _PasswordRequested:
        raise TLSError(f"{display_path(key_path)} unexpectedly asks for a passphrase.") from None
    except ssl.SSLError as exc:
        raise TLSError(f"the managed certificate could not be loaded: {exc}") from None
    except OSError as exc:
        raise TLSError(_os_problem(exc)) from exc
    except fs_boundary.FilesystemPolicyError as exc:
        raise TLSError(str(exc)) from exc
    return TLSMaterial(
        key_path=key_path,
        cert_path=cert_path,
        managed=True,
        regenerated=bool(reason),
        identities=_san_identities(certificate),
        reason=reason,
        fingerprint=_fingerprint(certificate),
        warnings=warnings,
    )


@contextlib.contextmanager
def certificate_lock():
    """Serialize checking, replacing and loading the managed pair.

    Two launches sharing one install — on two ports, say — must never interleave
    their writes into a key from one and a certificate from the other.
    """
    directory, _, _ = managed_paths()
    fs_boundary.ensure_internal_dir(directory, mode=0o700)
    handle = os.open(str(directory / LOCK_NAME), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        _acquire(handle)
        try:
            yield
        finally:
            _release(handle)
    finally:
        os.close(handle)


@contextlib.contextmanager
def holding(transport):
    """Keep the managed pair still while Uvicorn loads it. A no-op otherwise."""
    if transport is not None and transport.mode == MANAGED:
        with certificate_lock():
            yield
    else:
        yield


def _inspect(key_path, cert_path, required, now):
    """Why the managed pair cannot be used as it is ("" if it can), and its names."""
    has_key, has_cert = key_path.is_file(), cert_path.is_file()
    if not has_key and not has_cert:
        return FIRST_CERTIFICATE, ()
    if not has_cert:
        return f"{CERT_NAME} was missing", ()
    try:
        certificate = x509.load_pem_x509_certificate(cert_path.read_bytes())
    except (OSError, ValueError):
        return f"{CERT_NAME} could not be read", ()
    previous = _san_identities(certificate)
    if not has_key:
        return f"{KEY_NAME} was missing", previous
    try:
        key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    except (OSError, ValueError, TypeError, UnsupportedAlgorithm):
        return f"{KEY_NAME} could not be read", previous
    if _public_der(key.public_key()) != _public_der(certificate.public_key()):
        return "the key and certificate did not match", previous
    if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < RSA_KEY_SIZE:
        return f"the key was weaker than {RSA_KEY_SIZE}-bit RSA", previous
    starts, ends = _validity(certificate)
    if starts > now:
        return f"the old one only starts on {starts:%Y-%m-%d} (was the clock wrong?)", previous
    if ends <= now:
        return f"the old one expired on {ends:%Y-%m-%d}", previous
    if ends - now <= RENEW_BEFORE:
        return f"the old one expires on {ends:%Y-%m-%d}", previous
    if not previous:
        return "the old one named no addresses", previous
    missing = [name for name in required if name not in previous]
    if missing:
        return f"the old one did not cover {', '.join(missing)}", previous
    return "", previous


def _build_pair(identities, now):
    """A fresh RSA key and a self-signed leaf certificate naming ``identities``."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=RSA_KEY_SIZE)
    primary = next((name for name in identities if name not in LOOPBACK_IDENTITIES),
                   "localhost")
    subject = x509.Name([
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Video Trim"),
        x509.NameAttribute(NameOID.COMMON_NAME, primary[:64]),
    ])
    key_id = x509.SubjectKeyIdentifier.from_public_key(key.public_key())
    usage = x509.KeyUsage(
        digital_signature=True, key_encipherment=True, content_commitment=False,
        data_encipherment=False, key_agreement=False, key_cert_sign=False,
        crl_sign=False, encipher_only=False, decipher_only=False,
    )
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        # Random, never reused: Firefox refuses — with no way past the warning —
        # a certificate whose issuer and serial match one it has already seen.
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - BACKDATE)
        .not_valid_after(now + VALIDITY)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(usage, critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(key_id, critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(key_id),
            critical=False,
        )
        .add_extension(
            x509.SubjectAlternativeName([_general_name(name) for name in identities]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return key_pem, certificate.public_bytes(serialization.Encoding.PEM)


def _publish(directory, pair, key_path, cert_path):
    """Stage both files beside their targets, then move each into place whole.

    Neither file is ever half-written. A crash between the two moves leaves a
    key and certificate that do not match, which the next launch's check catches
    and replaces, so the pair heals rather than needing a two-file transaction.
    """
    key_pem, cert_pem = pair
    fs_boundary.ensure_internal_dir(directory, mode=0o700)
    staged = []
    try:
        staged.append(_stage(directory, key_pem, 0o600))
        staged.append(_stage(directory, cert_pem, 0o644))
        _replace(staged[0], key_path)
        _replace(staged[1], cert_path)
    finally:
        for temporary in staged:
            if temporary.exists():
                fs_boundary.safe_internal_unlink(temporary)


def _stage(directory, data, mode):
    # mkstemp creates the file 0600, so the key is never readable by anyone
    # else, not even for the moment before a chmod.
    descriptor, name = tempfile.mkstemp(prefix=_TEMP_PREFIX, suffix=_TEMP_SUFFIX,
                                        dir=str(directory))
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name == "posix":
            os.chmod(temporary, mode)
    except BaseException:
        fs_boundary.safe_internal_unlink(temporary)
        raise
    return temporary


def _replace(source, target):
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            # On Windows a virus scanner often holds a file it has just seen
            # written, and replacing it fails until the scanner lets go.
            if os.name != "nt" or attempt == _REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(0.1 * (attempt + 1))


def _restrict_key(key_path):
    """Owner-only on the managed key, re-applied on every start. Returns warnings."""
    if os.name != "posix":
        # On Windows the install directory's ACLs decide, as they do for app.db.
        return ()
    os.chmod(key_path, 0o600)
    if key_path.stat().st_mode & 0o077:
        return (
            f"{display_path(key_path)} could not be made private to your user (this "
            "drive ignores permissions), so other accounts on this machine may be "
            "able to read it.",
        )
    return ()


def _sweep_temporaries(directory):
    """Remove files a crashed launch staged but never moved into place.

    Safe because staging only ever happens under the lock this runs inside.
    """
    if not directory.is_dir():
        return
    for leftover in directory.glob(f"{_TEMP_PREFIX}*{_TEMP_SUFFIX}"):
        fs_boundary.safe_internal_unlink(leftover)


def _acquire(handle):
    deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
    while True:
        try:
            if os.name == "nt":  # pragma: no cover - Windows only
                import msvcrt

                os.lseek(handle, 0, os.SEEK_SET)
                msvcrt.locking(handle, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError:
            if time.monotonic() > deadline:
                raise TLSError(
                    "another Video Trim is still preparing its HTTPS certificate. "
                    "Wait for it to finish starting, then try again."
                ) from None
            time.sleep(0.05)


def _release(handle):
    if os.name == "nt":  # pragma: no cover - Windows only
        import msvcrt

        os.lseek(handle, 0, os.SEEK_SET)
        msvcrt.locking(handle, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle, fcntl.LOCK_UN)


# --- a supplied pair -------------------------------------------------------------
def validate_user_pair(key_path, cert_path, now=None):
    """Check a host-supplied pair before any listener opens. Only ever reads it.

    OpenSSL decides whether the pair loads, because that is what Uvicorn will
    use; what cryptography can read about the certificate only produces
    warnings. An expired certificate, or one for another name, may still be
    what the host intends, so neither stops the launch.
    """
    key_file = _supplied(key_path, "--tls-keyfile")
    cert_file = _supplied(cert_path, "--tls-certfile")
    try:
        _openssl_load(key_file, cert_file)
    except _PasswordRequested:
        raise TLSError(
            f"{key_file} is protected by a passphrase, which Video Trim cannot use "
            "yet. Give it a copy without one (for example: openssl pkey -in "
            "encrypted.key -out plain.key) and keep that copy private."
        ) from None
    except ssl.SSLError as exc:
        raise TLSError(_supplied_problem(key_file, cert_file, exc)) from None
    except OSError as exc:
        raise TLSError(_os_problem(exc)) from None

    warnings = []
    identities = ()
    fingerprint = ""
    certificate = _leaf(cert_file)
    if certificate is None:
        warnings.append("The certificate's details could not be read, so Video Trim "
                        "cannot tell which addresses it covers or when it expires.")
    else:
        identities = _san_identities(certificate, wildcards=True)
        fingerprint = _fingerprint(certificate)
        now = now or _utcnow()
        starts, ends = _validity(certificate)
        if ends <= now:
            warnings.append(f"The certificate expired on {ends:%Y-%m-%d}, so browsers "
                            "will refuse it.")
        elif starts > now:
            warnings.append(f"The certificate only starts on {starts:%Y-%m-%d}, so "
                            "browsers will refuse it.")
        elif ends - now <= RENEW_BEFORE:
            warnings.append(f"The certificate expires on {ends:%Y-%m-%d}.")
        if not identities:
            warnings.append("The certificate names no addresses (no subjectAltName), "
                            "so browsers will not accept it for any URL.")
    return TLSMaterial(
        key_path=key_file,
        cert_path=cert_file,
        managed=False,
        identities=identities,
        fingerprint=fingerprint,
        warnings=tuple(warnings),
    )


def _supplied(value, flag):
    path = Path(os.path.expanduser(str(value)))
    if not path.is_absolute():
        path = Path.cwd() / path
    try:
        if not path.exists():
            raise TLSError(f"{flag} {path}: no such file.")
        if not path.is_file():
            raise TLSError(f"{flag} {path} is not a file.")
    except OSError as exc:
        raise TLSError(f"{flag} {path}: {exc.strerror or exc}") from None
    return path


def _supplied_problem(key_file, cert_file, exc):
    if getattr(exc, "reason", "") == "KEY_VALUES_MISMATCH":
        return f"the key in {key_file} does not belong to the certificate in {cert_file}."
    if x509 is not None and _leaf(cert_file) is None:
        return f"{cert_file} is not a PEM certificate."
    if not _looks_like_key(key_file):
        return f"{key_file} is not a PEM private key."
    return f"{cert_file} and {key_file} could not be loaded: {exc}"


def _leaf(path):
    """The first certificate in a PEM file — the one a server presents — or None."""
    if x509 is None:
        return None
    try:
        return x509.load_pem_x509_certificates(Path(path).read_bytes())[0]
    except Exception:  # anything unreadable is reported as unreadable
        return None


def _looks_like_key(path):
    if serialization is None:
        return True
    try:
        serialization.load_pem_private_key(Path(path).read_bytes(), password=None)
    except TypeError:
        return True  # a key, just an encrypted one
    except Exception:
        return False
    return True


# --- shared helpers --------------------------------------------------------------
def _openssl_load(key_path, cert_path):
    """Load the pair exactly as Uvicorn will; raises if OpenSSL refuses it."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(cert_path), str(key_path), password=_refuse_password)


def _refuse_password():
    # Without a callback OpenSSL asks for the passphrase on the console and the
    # launch sits waiting, or fails with a baffling error when there is none.
    raise _PasswordRequested()


def _san_identities(certificate, wildcards=False):
    """The names a certificate covers, spelled as canonical_identity spells them."""
    try:
        names = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except Exception:  # absent or malformed: either way it names nothing
        return ()
    found = []
    for value in names.get_values_for_type(x509.DNSName):
        text = str(value).strip().lower().rstrip(".")
        if wildcards and text.startswith("*."):
            rest = _dns_name(text[2:])
            found.append(f"*.{rest}" if rest else "")
        else:
            found.append(_dns_name(text))
    for value in names.get_values_for_type(x509.IPAddress):
        if isinstance(value, (ipaddress.IPv4Address, ipaddress.IPv6Address)):
            found.append(canonical_identity(str(value)))
    return _unique(found)


def _matches(pattern, name):
    if pattern == name:
        return True
    # A supplied certificate may carry a wildcard, which covers exactly one label.
    if pattern.startswith("*.") and _ip(name) is None and "." in name:
        return name.split(".", 1)[1] == pattern[2:]
    return False


def _discovered_ip(value):
    """A discovered address worth naming in a certificate, or ""."""
    address = _ip(_unbracket(str(value or "").strip()))
    if address is None:
        return ""
    if (address.is_unspecified or address.is_loopback or address.is_multicast
            or address.is_link_local or address.is_reserved):
        return ""
    return str(address)


def _ip(text):
    try:
        address = ipaddress.ip_address(str(text).split("%", 1)[0])
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        return address.ipv4_mapped
    return address


def _dns_name(value):
    """A DNS name a certificate can hold — lower case, IDNA-encoded — or ""."""
    name = str(value or "").strip().rstrip(".").lower()
    if not name:
        return ""
    try:
        name = name.encode("idna").decode("ascii")
    except UnicodeError:
        return ""
    if len(name) > 253 or not all(_DNS_LABEL.match(label) for label in name.split(".")):
        return ""
    return name


def _general_name(name):
    address = _ip(name)
    return x509.IPAddress(address) if address is not None else x509.DNSName(name)


def _unbracket(text):
    if text.startswith("[") and text.endswith("]"):
        return text[1:-1]
    return text


def _unique(values):
    seen = []
    for value in values:
        if value and value not in seen:
            seen.append(value)
    return tuple(seen)


def _hostname():
    try:
        return socket.gethostname()
    except OSError:
        return ""


def _utcnow():
    return datetime.datetime.now(datetime.timezone.utc)


def _validity(certificate):
    return certificate.not_valid_before_utc, certificate.not_valid_after_utc


def _public_der(public_key):
    return public_key.public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )


def _fingerprint(certificate):
    return ":".join(f"{byte:02X}" for byte in certificate.fingerprint(hashes.SHA256()))


def display_path(path):
    """A path as the host's console should show it: relative when it is ours."""
    try:
        return Path(path).resolve().relative_to(fs_boundary.INSTALL_ROOT).as_posix()
    except (OSError, ValueError):
        return str(path)


def _os_problem(exc):
    target = getattr(exc, "filename2", None) or getattr(exc, "filename", None)
    detail = exc.strerror or str(exc)
    return f"{display_path(target)}: {detail}" if target else detail
