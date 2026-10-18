"""Bounded HTTPX retrieval with per-request DNS validation and IP-pinned TCP."""

from __future__ import annotations

import asyncio
import ipaddress
import math
import re
import socket
import ssl
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit

import anyio
import httpcore
import httpx
from anyio.streams.tls import TLSAttribute, TLSStream

from jarvis.tools.browser.models import ResearchRequest, ResearchResult, ResearchSource

DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_RESPONSE_BYTES = 1_000_000
DEFAULT_MAX_TEXT_CHARS = 50_000
DEFAULT_MAX_EXCERPT_CHARS = 800
DEFAULT_MAX_SUMMARY_CHARS = 1_200
MAX_DNS_ADDRESSES = 32
_TLS12_CIPHER_POLICY = "ECDHE+AESGCM:ECDHE+CHACHA20:!aNULL:!MD5:!RC4:!CBC"
_LOCAL_SUFFIXES = (".localhost", ".local", ".internal", ".home.arpa", ".test", ".invalid")
_NON_CONTENT_TAGS = {
    "script", "style", "noscript", "template", "svg", "iframe", "canvas", "nav", "header",
    "footer",
}
_BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "br", "dd", "div", "dl", "dt",
    "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr",
    "li", "main", "nav", "ol", "p", "pre", "section", "table", "td", "th", "tr", "ul",
}
Resolver = Callable[[str], Awaitable[Iterable[str]]]


class ResearchFetchError(Exception):
    """Base class for safe, predictable retrieval failures."""


class ResearchNavigationError(ResearchFetchError):
    """The URL, DNS result, or a server redirect did not satisfy navigation policy."""


class ResearchDNSResolutionError(ResearchFetchError):
    """The trusted hostname could not be resolved to a safe public address."""


class ResearchTimeoutError(ResearchFetchError):
    """The request or DNS resolution exceeded the configured timeout."""


class ResearchNetworkError(ResearchFetchError):
    """A network operation failed without exposing internal details."""


class ResearchHTTPError(ResearchFetchError):
    """The remote server returned an unsuccessful HTTP status."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__("Research server returned an HTTP error.")


class ResearchResponseTooLargeError(ResearchFetchError):
    """The response body exceeded the configured byte limit."""


class ResearchUnsupportedContentTypeError(ResearchFetchError):
    """The response media type is not supported."""


class ResearchContentError(ResearchFetchError):
    """The response had no useful extractable text."""


def _normalize_hostname(hostname: str) -> str:
    candidate = hostname.rstrip(".").lower()
    if not candidate or len(candidate) > 253:
        raise ValueError("Invalid hostname")
    try:
        candidate = candidate.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("Invalid hostname") from exc
    labels = candidate.split(".")
    if any(
        not label
        or len(label) > 63
        or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label)
        for label in labels
    ):
        raise ValueError("Invalid hostname")
    if candidate == "localhost" or candidate.endswith(_LOCAL_SUFFIXES):
        raise ValueError("Local hostnames are not permitted")
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        return candidate
    raise ValueError("IP-literal destinations are not permitted")


def _safe_url(url: str, allowed_domains: frozenset[str]) -> str:
    if any(ord(char) < 32 or char.isspace() for char in url):
        raise ResearchNavigationError("Research URL is invalid or not permitted.")
    try:
        parts = urlsplit(url)
        if parts.scheme.lower() != "https" or not parts.netloc:
            raise ValueError("Unsupported URL")
        if parts.username is not None or parts.password is not None or "\\" in parts.netloc:
            raise ValueError("URL credentials or ambiguous authority")
        hostname = parts.hostname
        if hostname is None or _normalize_hostname(hostname) not in allowed_domains:
            raise ValueError("Hostname is not allowed")
        if parts.port not in (None, 443):
            raise ValueError("Nonstandard HTTPS port")
    except (ValueError, UnicodeError) as exc:
        raise ResearchNavigationError("Research URL is invalid or not permitted.") from exc
    return urlunsplit(("https", parts.netloc, parts.path or "/", parts.query, ""))


def _validated_addresses(
    addresses: Iterable[str], approved_addresses: frozenset[str]
) -> tuple[str, ...]:
    validated: list[str] = []
    try:
        for index, raw_address in enumerate(addresses):
            if index >= MAX_DNS_ADDRESSES:
                raise ValueError("Too many DNS results")
            if "%" in raw_address:
                raise ValueError("Scoped addresses are not permitted")
            address = ipaddress.ip_address(raw_address)
            if address.version == 6 and address.ipv4_mapped is not None:
                address = address.ipv4_mapped
            if not _is_public_address(address) or str(address) not in approved_addresses:
                raise ValueError("DNS result is not approved")
            normalized = str(address)
            if normalized not in validated:
                validated.append(normalized)
    except (TypeError, ValueError) as exc:
        raise ResearchNavigationError("Research host resolved to a prohibited address.") from exc
    if not validated:
        raise ResearchDNSResolutionError("Research hostname returned no addresses.")
    return tuple(validated)


def _is_public_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        address.is_global
        and not address.is_private
        and not address.is_loopback
        and not address.is_link_local
        and not address.is_reserved
        and not address.is_multicast
        and not address.is_unspecified
    )


def _configured_addresses(addresses: Iterable[str]) -> frozenset[str]:
    if isinstance(addresses, (str, bytes)):
        raise ValueError("Research IP policy must contain exact IP addresses.")
    normalized: set[str] = set()
    try:
        for raw_address in addresses:
            if not isinstance(raw_address, str) or "%" in raw_address:
                raise ValueError("Malformed IP address")
            address = ipaddress.ip_address(raw_address)
            if address.version == 6 and address.ipv4_mapped is not None:
                address = address.ipv4_mapped
            canonical = str(address)
            if not _is_public_address(address) or canonical in normalized:
                raise ValueError("IP address is not permitted or is duplicated")
            normalized.add(canonical)
    except (TypeError, ValueError) as exc:
        raise ValueError("Research IP policy must contain unique, globally routable IPs.") from exc
    if not normalized:
        raise ValueError("Research IP policy must not be empty.")
    return frozenset(normalized)


async def _system_resolver(hostname: str) -> tuple[str, ...]:
    loop = asyncio.get_running_loop()
    results = await loop.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
    return tuple(result[4][0] for result in results)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text_parts: list[str] = []
        self.title_parts: list[str] = []
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag == "title":
            self._in_title = True
            return
        if tag in _NON_CONTENT_TAGS | {"head"}:
            self._skip_depth += 1
        elif not self._skip_depth and tag in _BLOCK_TAGS:
            self.text_parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
            return
        if tag in _NON_CONTENT_TAGS | {"head"}:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif not self._skip_depth and tag in _BLOCK_TAGS:
            self.text_parts.append(" ")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)
        elif not self._skip_depth:
            self.text_parts.append(data)


def _clean_text(parts: Iterable[str]) -> str:
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


def _summary(text: str, limit: int) -> str:
    sentences = re.split(r"(?<=[.!?])\s+", text)
    selected = ""
    for sentence in sentences:
        candidate = f"{selected} {sentence}".strip()
        if len(candidate) > limit:
            break
        selected = candidate
    return (selected or text[:limit]).strip()


class _PinnedNetworkStream(httpcore.AsyncNetworkStream):
    def __init__(self, stream: anyio.abc.ByteStream) -> None:
        self._stream = stream

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        try:
            with anyio.fail_after(timeout):
                try:
                    return await self._stream.receive(max_bytes)
                except anyio.EndOfStream:
                    return b""
        except TimeoutError as exc:
            raise httpcore.ReadTimeout("Research socket read timed out.") from exc
        except Exception as exc:
            raise httpcore.ReadError("Research socket read failed.") from exc

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        try:
            with anyio.fail_after(timeout):
                await self._stream.send(buffer)
        except TimeoutError as exc:
            raise httpcore.WriteTimeout("Research socket write timed out.") from exc
        except Exception as exc:
            raise httpcore.WriteError("Research socket write failed.") from exc

    async def aclose(self) -> None:
        await self._stream.aclose()

    async def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> _PinnedNetworkStream:
        try:
            with anyio.fail_after(timeout):
                secure_stream = await TLSStream.wrap(
                    self._stream,
                    hostname=server_hostname,
                    ssl_context=ssl_context,
                    standard_compatible=True,
                )
        except TimeoutError as exc:
            raise httpcore.ConnectTimeout("Research TLS handshake timed out.") from exc
        except Exception as exc:
            raise httpcore.ConnectError("Research TLS handshake failed.") from exc
        return _PinnedNetworkStream(secure_stream)

    def get_extra_info(self, info: str) -> object | None:
        if info == "ssl_object" and isinstance(self._stream, TLSStream):
            return self._stream.extra(TLSAttribute.ssl_object)
        return None


class _PinnedNetworkBackend(httpcore.AsyncNetworkBackend):
    """Connect only to this fetch's already validated numeric DNS answers."""

    def __init__(self, hostname: str, addresses: tuple[str, ...]) -> None:
        self._hostname = hostname
        self._addresses = addresses

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[tuple[object, ...]] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        del socket_options
        if _normalize_hostname(host) != self._hostname or port != 443:
            raise httpcore.ConnectError("Research connection origin did not match policy.")
        address = self._addresses[0]
        try:
            with anyio.fail_after(timeout):
                stream = await anyio.connect_tcp(
                    remote_host=address,
                    remote_port=port,
                    local_host=local_address,
                )
        except TimeoutError as exc:
            raise httpcore.ConnectTimeout("Research connection timed out.") from exc
        except Exception as exc:
            raise httpcore.ConnectError("Research connection failed.") from exc
        return _PinnedNetworkStream(stream)

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[tuple[object, ...]] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        del path, timeout, socket_options
        raise httpcore.ConnectError("Research Unix socket destinations are not permitted.")

    async def sleep(self, seconds: float) -> None:
        await anyio.sleep(seconds)


class _CoreByteStream(Protocol):
    def __aiter__(self) -> AsyncIterator[bytes]: ...

    async def aclose(self) -> None: ...


class _CoreResponseStream(httpx.AsyncByteStream):
    def __init__(self, stream: _CoreByteStream) -> None:
        self._stream = stream

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._stream:
            yield chunk

    async def aclose(self) -> None:
        await self._stream.aclose()


class _PinnedHTTPTransport(httpx.AsyncBaseTransport):
    """One-fetch HTTPX transport with keep-alive disabled for DNS-pin isolation.

    Do not reuse this transport or its connection pool across fetches without a
    security-reviewed design that preserves each fetch's validated DNS pin.
    """

    def __init__(self, hostname: str, addresses: tuple[str, ...]) -> None:
        ssl_context = ssl.create_default_context()
        if ssl_context.minimum_version < ssl.TLSVersion.TLSv1_2:
            ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2
        # OpenSSL applies set_ciphers to TLS 1.2 and earlier; TLS 1.3 remains
        # governed by the platform's secure defaults.
        ssl_context.set_ciphers(_TLS12_CIPHER_POLICY)
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl_context,
            max_connections=1,
            # Each fetch owns a fresh pool; idle connections must not outlive it.
            max_keepalive_connections=0,
            retries=0,
            network_backend=_PinnedNetworkBackend(hostname, addresses),
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        core_request = httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=request.url.raw_scheme,
                host=request.url.raw_host,
                port=request.url.port,
                target=request.url.raw_path,
            ),
            headers=request.headers.raw,
            content=request.stream,
            extensions=request.extensions,
        )
        try:
            response = await self._pool.handle_async_request(core_request)
        except httpcore.TimeoutException as exc:
            raise httpx.TimeoutException("Research request timed out.", request=request) from exc
        except httpcore.HTTPError as exc:
            raise httpx.NetworkError("Research request failed.", request=request) from exc
        return httpx.Response(
            status_code=response.status,
            headers=response.headers,
            stream=_CoreResponseStream(response.stream),
            extensions=response.extensions,
        )

    async def aclose(self) -> None:
        await self._pool.aclose()


class ResearchFetcher:
    """Fetch one HTTPS page through a per-request, DNS-pinned direct connection.

    Each fetch creates and closes a fresh HTTPX client and pinned transport;
    connection keep-alive is disabled so a later fetch cannot reuse a socket
    associated with an earlier DNS answer. Do not introduce client or transport
    reuse without a security-reviewed per-fetch pinning design.

    Each fetch resolves the allowlisted hostname and requires every answer to
    be a globally routable address in that hostname's trusted exact-IP policy.
    The original hostname remains the TLS SNI and certificate-verification name
    and the HTTP Host value. TLS uses the platform trust store with a TLS 1.2
    minimum. Redirects are rejected. This narrows DNS-rebinding risk but is not
    comprehensive SSRF protection: network routing and approved but hostile or
    compromised endpoints remain outside this fetcher's control.
    """

    def __init__(
        self,
        allowed_domains: Iterable[str],
        *,
        allowed_ips_by_domain: Mapping[str, Iterable[str]],
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        max_text_chars: int = DEFAULT_MAX_TEXT_CHARS,
        max_excerpt_chars: int = DEFAULT_MAX_EXCERPT_CHARS,
        max_summary_chars: int = DEFAULT_MAX_SUMMARY_CHARS,
        resolver: Resolver | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        try:
            domain_entries = tuple(_normalize_hostname(domain) for domain in allowed_domains)
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("Research allowlist must contain valid public hostnames.") from exc
        normalized_domains = frozenset(domain_entries)
        if not normalized_domains:
            raise ValueError("Research allowlist must not be empty.")
        if len(domain_entries) != len(normalized_domains):
            raise ValueError("Research allowlist contains duplicate normalized hostnames.")
        if not isinstance(allowed_ips_by_domain, Mapping):
            raise ValueError("Research IP policy must map allowed hostnames to exact IPs.")
        normalized_ip_policy: dict[str, frozenset[str]] = {}
        try:
            for domain, addresses in allowed_ips_by_domain.items():
                normalized_domain = _normalize_hostname(domain)
                if normalized_domain in normalized_ip_policy:
                    raise ValueError("Research IP policy contains duplicate normalized hostnames.")
                normalized_ip_policy[normalized_domain] = _configured_addresses(addresses)
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("Research IP policy is invalid.") from exc
        if normalized_ip_policy.keys() != normalized_domains:
            raise ValueError("Research IP policy must exactly match the allowed hostnames.")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("Research timeout must be positive.")
        if min(max_response_bytes, max_text_chars, max_excerpt_chars, max_summary_chars) < 1:
            raise ValueError("Research size limits must be positive.")
        if max_text_chars > 100_000 or max_excerpt_chars > 800 or max_summary_chars > 1_200:
            raise ValueError("Research size limits exceed their hard maximum.")
        self._allowed_domains = normalized_domains
        self._allowed_ips_by_domain = normalized_ip_policy
        self._timeout_seconds = timeout_seconds
        self._max_response_bytes = max_response_bytes
        self._max_text_chars = max_text_chars
        self._max_excerpt_chars = max_excerpt_chars
        self._max_summary_chars = max_summary_chars
        if resolver is not None and not callable(resolver):
            raise ValueError("Research resolver must be callable.")
        self._resolver = _system_resolver if resolver is None else resolver
        self._clock = clock or (lambda: datetime.now(UTC))

    @property
    def allowed_domains(self) -> frozenset[str]:
        """Return the immutable trusted hostname allowlist."""

        return self._allowed_domains

    async def _resolve(self, hostname: str) -> tuple[str, ...]:
        try:
            addresses = await asyncio.wait_for(
                self._resolver(hostname), timeout=self._timeout_seconds
            )
        except TimeoutError as exc:
            raise ResearchTimeoutError("Research DNS resolution timed out.") from exc
        except Exception as exc:
            raise ResearchDNSResolutionError("Research hostname could not be resolved.") from exc
        return _validated_addresses(addresses, self._allowed_ips_by_domain[hostname])

    async def fetch(self, request: ResearchRequest) -> ResearchResult:
        url = _safe_url(request.url, self._allowed_domains)
        hostname = _normalize_hostname(urlsplit(url).hostname or "")
        addresses = await self._resolve(hostname)
        timeout = httpx.Timeout(self._timeout_seconds)
        transport = _PinnedHTTPTransport(hostname, addresses)
        try:
            async with httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=False,
                trust_env=False,
                transport=transport,
            ) as client:
                async with client.stream("GET", url) as response:
                    if 300 <= response.status_code < 400:
                        raise ResearchNavigationError("Research redirects are not followed.")
                    if response.status_code >= 400:
                        raise ResearchHTTPError(response.status_code)
                    content_type = response.headers.get("content-type", "").split(";", 1)[0]
                    if content_type.strip().lower() not in {
                        "text/html", "application/xhtml+xml", "text/plain"
                    }:
                        raise ResearchUnsupportedContentTypeError(
                            "Research response content type is not supported."
                        )
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > self._max_response_bytes:
                            raise ResearchResponseTooLargeError(
                                "Research response exceeded the configured byte limit."
                            )
                        body.extend(chunk)
                    encoding = response.encoding or "utf-8"
                    try:
                        document = body.decode(encoding)
                    except (LookupError, UnicodeDecodeError) as exc:
                        raise ResearchContentError("Research response text could not be decoded.") from exc
        except httpx.TimeoutException as exc:
            raise ResearchTimeoutError("Research request timed out.") from exc
        except httpx.HTTPError as exc:
            raise ResearchNetworkError("Research request failed.") from exc

        if content_type.strip().lower() == "text/plain":
            title = ""
            extracted = _clean_text((document,))
        else:
            parser = _TextExtractor()
            try:
                parser.feed(document)
                parser.close()
            except (ValueError, AssertionError) as exc:
                raise ResearchContentError("Research page could not be parsed.") from exc
            title = _clean_text(parser.title_parts)
            extracted = _clean_text(parser.text_parts)
        if len(re.sub(r"[^\w]", "", extracted, flags=re.UNICODE)) < 3:
            raise ResearchContentError("Research page contained no useful text.")

        bounded_text = extracted[: self._max_text_chars].rstrip()
        if not bounded_text:
            raise ResearchContentError("Research page contained no useful text.")
        text_truncated = len(bounded_text) < len(extracted)
        confidence = len(bounded_text) / len(extracted) if text_truncated else 1.0
        timestamp = self._clock()
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("Research clock must return a timezone-aware datetime.")
        source = ResearchSource(
            title=(title or hostname)[:300],
            url=url,
            retrieved_at=timestamp.astimezone(UTC),
            evidence_excerpt=bounded_text[: self._max_excerpt_chars],
            extractive_summary=_summary(bounded_text, self._max_summary_chars),
            confidence=confidence,
            text_truncated=text_truncated,
        )
        return ResearchResult(source=source, extracted_text=bounded_text)
