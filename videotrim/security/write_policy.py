"""Which authenticated client may cause the host to write anything.

Authentication answers "who can ask". This answers "whose asking may put bytes
on the host's disk". Neither answers "what filesystem operations exist" — only
fs_boundary does that, and no answer here can widen it.

A new remote address starts denied. The host flips it on from the machine
itself, and the change takes effect on the very next check with no restart.
"""

from .network import normalize_ip


class WriteDecision:
    """Why a write was allowed or refused, in words a person can act on."""

    def __init__(self, allowed, reason="", can_request=False, requested=False):
        self.allowed = bool(allowed)
        self.reason = reason
        self.can_request = bool(can_request)
        self.requested = bool(requested)

    def __bool__(self):
        return self.allowed


DENIED_MESSAGE = "File transfer disabled by host"

IP_DRIFT_NOTE = (
    "Permission is tied to this device's network address. If the address "
    "changes, the host will need to allow it again."
)


class WritePolicy:
    """Per-IP write authorization, re-read from the store on every check."""

    def __init__(self, store, host_guard):
        self._store = store
        self._host = host_guard

    def evaluate(self, request, session):
        """The decision for one request. Cheap enough to call twice per job.

        It is called twice on purpose: once when work starts, and again
        immediately before anything leaves the internal staging area, so a
        permission the host revoked during a long render is still honoured.
        """
        if session is None:
            return WriteDecision(False, "Login required.")

        if self._host.is_host_request(request):
            return WriteDecision(True, "Host machine.")

        address = normalize_ip(getattr(getattr(request, "client", None), "host", ""))
        if not address:
            return WriteDecision(False, DENIED_MESSAGE, can_request=False)

        row = self._store.ip_row(address)
        if row is not None and row["write_allowed"]:
            return WriteDecision(True, "Allowed by the host.")

        return WriteDecision(
            False,
            DENIED_MESSAGE,
            can_request=True,
            requested=bool(row and row["access_requested_at"]),
        )

    def allowed(self, request, session):
        return self.evaluate(request, session).allowed

    def authorizer(self, request, session):
        """A callable the output gateway invokes just before the external create.

        Returning a closure rather than a boolean is the point: the gateway calls
        it at commit time, not at request time, so revocation lands even mid-job.
        """

        def check():
            decision = self.evaluate(request, session)
            return decision.allowed, decision.reason

        return check

    def request_access(self, request, session, throttle):
        """Record that a denied client asked. Grants nothing.

        There is no remote approval path, no approval token, no auto-expiry into
        an allow state. The host acts on it from their own machine or it does
        not happen.
        """
        address = normalize_ip(getattr(getattr(request, "client", None), "host", ""))
        if not address:
            return False
        if not throttle.allow(address):
            return True  # already asked recently; stay idempotent and quiet
        self._store.request_access(address, session.username if session else "")
        return True
