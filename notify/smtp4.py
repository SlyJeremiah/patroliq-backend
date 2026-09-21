"""
SMTP email backend that connects over IPv4 only (``EMAIL_FORCE_IPV4``, default on).

Hosts such as Render have no IPv6 egress, while ``smtp.gmail.com`` and other providers publish AAAA
records first; Python's ``socket.create_connection`` then fails with ``[Errno 101] Network is
unreachable`` on the IPv6 address. Resolving the SMTP host to IPv4 addresses only avoids that.
"""
from __future__ import annotations

import smtplib
import socket

from django.core.mail.backends.smtp import EmailBackend as DjangoSMTPBackend


def _ipv4_connection(host: str, port: int, timeout, source_address=None) -> socket.socket:
    """Like ``socket.create_connection`` but only tries the host's IPv4 (A) addresses."""
    last_error: OSError | None = None
    for family, socktype, proto, _canon, sockaddr in socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM):
        sock = socket.socket(family, socktype, proto)
        try:
            if timeout is not None and timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:  # type: ignore[attr-defined]
                sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(sockaddr)
            return sock
        except OSError as exc:
            last_error = exc
            sock.close()
    raise last_error or OSError(f"no IPv4 address for {host}")


class _SMTP4(smtplib.SMTP):
    def _get_socket(self, host, port, timeout):
        return _ipv4_connection(host, port, timeout, self.source_address)


class _SMTP4SSL(smtplib.SMTP_SSL):
    def _get_socket(self, host, port, timeout):
        sock = _ipv4_connection(host, port, timeout, self.source_address)
        return self.context.wrap_socket(sock, server_hostname=self._host)


class EmailBackend(DjangoSMTPBackend):
    @property
    def connection_class(self):
        return _SMTP4SSL if self.use_ssl else _SMTP4
