"""Who is asking, and from where.

Two predicates live here, and keeping them apart is the whole point of the
module. ``can_browse_host_paths`` is a *read* capability that ``--allow-remote-
files`` may widen. ``is_host_request`` is an *admin* capability that it may
never widen. The previous single ``is_local()`` conflated the two and returned
True for every caller once the flag was set, which meant one convenience flag
handed out administration.
"""

import ipaddress
import socket

LOOPBACK_NAMES = frozenset({"localhost", "::1", "127.0.0.1", "::ffff:127.0.0.1"})

# Headers a proxy sets. Never trusted by default: anyone can send them, and
# believing one would let a remote client claim to be the host.
_FORWARD_HEADERS = ("x-forwarded-for", "forwarded", "x-real-ip", "cf-connecting-ip")


def normalize_ip(raw):
    """One canonical spelling per address, for use as a permission key.

    ``::ffff:192.168.1.5`` and ``192.168.1.5`` are the same device; a dual-stack
    listener reports whichever it feels like. Storing both would give one device
    two different permission rows.
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    if text.startswith("[") and "]" in text:
        text = text[1 : text.index("]")]
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return text
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        return str(address.ipv4_mapped)
    return str(address)


def rate_limit_key(raw):
    """Group an address for throttling.

    IPv6 is normalized to its /64, because a single attacker with a routed /64
    otherwise gets an effectively unlimited supply of "new" addresses to spend
    login attempts from. IPv4 keys on the address itself.
    """
    normalized = normalize_ip(raw)
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return normalized
    if isinstance(address, ipaddress.IPv6Address):
        return str(ipaddress.ip_network(f"{address}/64", strict=False))
    return normalized


def is_loopback(raw):
    normalized = normalize_ip(raw)
    if normalized in LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def primary_address():
    """The IPv4 address this machine uses to reach its network, or "".

    On a normal home network it is the one other devices can reach, which is
    why the banner leads with it and the HTTPS certificate always covers it.
    Connecting a UDP socket only consults the routing table: no packet is sent,
    and nothing outside this machine is contacted.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))  # reserved documentation address; sends nothing
        return normalize_ip(probe.getsockname()[0])
    except OSError:
        return ""
    finally:
        probe.close()


def hostname_addresses(family=0):
    """Every address this machine's own hostname resolves to, best effort.

    Noisier than it looks: on Windows it usually includes virtual adapters
    (WSL, Hyper-V, VPNs) whose addresses change from one boot to the next.
    """
    found = []
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, family)
    except (OSError, socket.gaierror):
        return found
    for info in infos:
        address = normalize_ip(info[4][0])
        if address and address not in found:
            found.append(address)
    return found


def own_addresses():
    """Every address that means "this machine".

    The startup banner prints the host's LAN address, so opening that URL while
    sitting at the host must not look like a stranger — otherwise Settings
    vanish for the one person entitled to them.
    """
    found = set(hostname_addresses())
    found.add(primary_address())
    found.discard("")
    return frozenset(found)


def client_ip(request):
    """The direct peer address. Forwarded headers are deliberately ignored."""
    client = getattr(request, "client", None)
    return normalize_ip(getattr(client, "host", "") if client else "")


def has_forwarding_headers(request):
    """True when something between us and the client is rewriting the origin."""
    headers = getattr(request, "headers", {})
    return any(name in headers for name in _FORWARD_HEADERS)


class HostAdminGuard:
    """Decides host-local administrative authority, and nothing else.

    Deliberately ignorant of ``--allow-remote-files``. If a tunnel or proxy makes
    the origin ambiguous, this fails closed: Settings disappear rather than being
    handed to a caller whose real address we cannot see.
    """

    def __init__(self, tunnel_active=False, addresses=None):
        self.tunnel_active = bool(tunnel_active)
        self.addresses = frozenset(addresses) if addresses is not None else own_addresses()

    def is_host_request(self, request):
        if self.tunnel_active:
            # --share makes every request arrive from the tunnel client. There is
            # no way to tell the host apart, so nobody is the host.
            return False
        if has_forwarding_headers(request):
            return False
        address = client_ip(request)
        if not address:
            return False
        if is_loopback(address):
            return True
        return address in self.addresses


class BrowseGuard:
    """Decides who may enumerate names and open host paths read-only.

    This is the one ``--allow-remote-files`` widens. It grants reading and
    nothing else: no Settings, no credential change, no output-directory change,
    no IP allow/deny, and it cannot promote a media token issued to another
    session.
    """

    def __init__(self, host_guard, allow_remote_files=False):
        self._host = host_guard
        self.allow_remote_files = bool(allow_remote_files)

    def can_browse_host_paths(self, request):
        if self.allow_remote_files:
            return True
        return self._host.is_host_request(request)
