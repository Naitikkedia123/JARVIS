# Research and Browser Retrieval Foundation

Phase 06 Pass A adds a small HTTPX-based, read-only page fetcher. It is
independent of tool registration, the model gateway, task runtime, personal
accounts, and application routes. It does not call a model, search service, or
hosted summarizer, and it does not execute browser scripts.

## Data contract

`ResearchRequest` names one URL. `ResearchResult` contains bounded extracted
text and a `ResearchSource` with the title, normalized request URL, UTC retrieval
time, evidence excerpt, deterministic extractive summary, confidence, and
provenance. Retrieved page content is marked
`untrusted_external_webpage`; instruction-like text remains ordinary page data
and cannot grant permissions or invoke tools. The dispatcher integration tests
exercise this boundary separately from the fetcher unit tests.

Confidence describes extraction completeness only: `1.0` means the extracted
text fit within the configured text bound; a lower value is the retained-to-
extracted character ratio when truncation occurred. It does not measure whether
the page is truthful, accurate, authoritative, or current.

## Retrieval and security boundary

The fetcher requires trusted initialization to supply both an exact-hostname
allowlist and a non-empty exact-IP allowlist for each hostname, for example:

```python
ResearchFetcher(
    ["research.example.org"],
    allowed_ips_by_domain={"research.example.org": ["93.184.216.34"]},
)
```

Hostnames are normalized using case and IDNA rules. URL credentials,
non-HTTPS URLs, nonstandard HTTPS ports, IP literals, and common local
hostnames are rejected. Every fetch resolves the approved hostname and rejects
the entire DNS answer set if any result is malformed, non-public, or absent
from that host's configured exact-IP set. TCP connects only to the validated
numeric address through HTTPCore's network-backend interface, avoiding a
second hostname lookup at connection time. IP entries are individual addresses,
not CIDR ranges. A hostname cannot use another hostname's IP entries. The
original hostname is retained for TLS SNI, certificate verification, and the
HTTP `Host` header. Certificate and hostname verification use the platform
trust store with a TLS 1.2 minimum. `SSLContext.set_ciphers()` applies the
`ECDHE+AESGCM:ECDHE+CHACHA20:!aNULL:!MD5:!RC4:!CBC` policy to TLS 1.2 and earlier,
allowing ECDHE with AES-GCM or ChaCha20-Poly1305. It does not configure TLS 1.3
cipher suites; TLS 1.3 retains platform/OpenSSL defaults. Connections are
direct:
environment-derived proxies are disabled and the transport has no proxy
configured. Automatic redirects are disabled.

This exact-IP policy reduces the risk that DNS rebinding or a compromised DNS
answer redirects a request to an unapproved destination. It is deliberately
strict and may break when a site's CDN changes addresses. A trusted initializer
must verify and deliberately update the per-host IP list; the fetcher does not
silently widen it or fall back to a new DNS address. Host-level controls and
network egress policy should further restrict where the process can connect.

Trusted allowlist approval checklist:

- Approve each exact hostname and its exact public IP list using a trusted
  source, not DNS answers alone.
- Verify the site/operator relationship and that the site's HTTPS certificate
  identity matches the approved hostname.
- Reject private, loopback, link-local, reserved, multicast, unspecified,
  malformed, and otherwise unverified addresses.
- Review every address change deliberately; never auto-expand the list from DNS.
- Periodically review approvals and expire entries that are no longer verified.
- Use a host or network egress firewall as an independent restriction layer.

Every fetch constructs and closes a fresh HTTPX client and pinned transport;
keep-alive is disabled so a later fetch cannot reuse a connection pinned to an
earlier DNS answer. Reusing clients, transports, or their connection pools
requires a security-reviewed design that preserves the per-fetch DNS pin.

Redirect responses, unsuccessful HTTP responses, unsupported media types,
DNS/network timeouts, resolver failures, malformed URLs, and response-size
violations produce typed safe errors. Response bytes are bounded while
streamed, regardless of `Content-Length`. HTML is parsed as text without script
execution, and script, style, template, embedded, navigation, header/footer,
and head content are excluded. `text/plain` is the only other supported
format. The configured extracted-text limit may not exceed 100,000 characters;
longer page titles are truncated to the 300-character model limit. The excerpt
and summary also have independent bounds. The summary selects the first
complete sentences that fit, with a bounded prefix fallback for a long first
sentence.

The caller's wait for both the injected resolver and the default async
`loop.getaddrinfo` path is bounded by the configured timeout; a timeout becomes
a safe typed timeout error before TCP connection setup. Cancelling the await of
the operating system `getaddrinfo` work may not stop its underlying executor
thread immediately, so the system resolver is not claimed to be fully
cancellable. Adding a nested `asyncio.wait_for` around the same operation would
not itself terminate that underlying executor work.

This is a narrow retrieval boundary, not comprehensive SSRF protection. Code
enforces exact host-to-IP matching, HTTPS/TLS validation, direct IP-pinned TCP,
and disabled redirects and proxies. These controls do not protect against a
compromised or hostile endpoint whose address was explicitly approved, unusual
local routing, another network-level path, or a compromised host. Operating
system and network egress policy remain necessary for broader containment.
The fetcher makes no claim that external page claims are safe or true;
downstream consumers must preserve their untrusted provenance and keep
authorization separate from retrieved content.

Unit tests inject DNS answers and mock AnyIO's TCP/TLS stream boundary; they make
no DNS or HTTP network requests or live TLS handshake. They verify exact
per-host IP policy, the pinned numeric connection target, the TLS 1.2 minimum,
normal certificate verification, original hostname identity, and a fresh
no-keepalive transport for each fetch. Separate integration tests cover
dispatcher authorization and preservation of untrusted provenance.
