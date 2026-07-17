from __future__ import annotations

import ipaddress
import re
import socket
import time
from collections import deque
from dataclasses import dataclass, field
from html import unescape
from typing import Any, Callable
from urllib.parse import urljoin, urlparse, urlunparse
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup

from kol_search.models import Account, ContactPoint


EMAIL_RE = re.compile(r"(?<![\w.+-])([A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})(?![\w.-])", re.I)
URL_RE = re.compile(r"https?://[^\s<>()\[\]{}\"']+", re.I)
MARKDOWN_LINK_RE = re.compile(r"\[([^\]]{1,200})\]\(([^)\s]+)\)")
CONTACT_PATH_RE = re.compile(
    r"/(contact|about|media|press|partnership|partner|collab|business|联系|关于|合作|商务)", re.I
)
LINK_AGGREGATORS = {"linktr.ee", "beacons.ai", "bio.link", "linkin.bio", "solo.to", "allmylinks.com"}
X_HOSTS = {"x.com", "www.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com"}
SOCIAL_HOSTS: dict[str, str] = {
    "t.me": "telegram",
    "telegram.me": "telegram",
    "discord.gg": "discord",
    "discord.com": "discord",
    "linkedin.com": "linkedin",
    "www.linkedin.com": "linkedin",
    "youtube.com": "youtube",
    "www.youtube.com": "youtube",
    "youtu.be": "youtube",
}
USER_AGENT = "kol-search/0.1 (+public-contact-research)"


class UnsafeURLError(ValueError):
    pass


@dataclass
class CrawlResult:
    contacts: list[ContactPoint] = field(default_factory=list)
    pages_fetched: int = 0
    warnings: list[str] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)


@dataclass
class FetchedPage:
    url: str
    body: str
    status_code: int
    content_type: str
    fetch_provider: str = "direct"


class JinaReaderError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


class JinaReader:
    """Authenticated Jina Reader client used only as public webpage fallback."""

    retryable_statuses = {408, 429}
    non_retryable_statuses = {400, 401, 403, 404}

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://r.jina.ai",
        timeout: float = 20.0,
        max_retries: int = 1,
        max_content_bytes: int = 2_000_000,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not api_key:
            raise ValueError("JINA_API_KEY is required when Jina Reader is enabled")
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("JINA_READER_BASE_URL must be an HTTP(S) URL without credentials")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.max_retries = max(0, min(3, max_retries))
        self.max_content_bytes = max_content_bytes
        self.sleep = sleep
        self.client = client or httpx.Client(timeout=timeout, follow_redirects=False)
        self._owns_client = client is None
        self.request_count = 0

    @classmethod
    def from_settings(
        cls,
        settings: Any,
        *,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> tuple[JinaReader | None, str | None]:
        ready, reason = settings.jina_reader_ready()
        if not settings.jina_reader_enabled:
            return None, None
        if not ready:
            return None, reason or "Jina Reader is not configured"
        return (
            cls(
                api_key=settings.jina_api_key or "",
                base_url=settings.jina_reader_base_url,
                timeout=settings.jina_reader_timeout_seconds,
                max_retries=settings.jina_reader_max_retries,
                max_content_bytes=settings.jina_reader_max_content_bytes,
                client=client,
                sleep=sleep,
            ),
            None,
        )

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def _reader_url(self, target_url: str) -> str:
        return f"{self.base_url}/{target_url}"

    @staticmethod
    def _retry_after_seconds(response: httpx.Response) -> float:
        value = response.headers.get("retry-after")
        if not value:
            return 0.25
        try:
            return max(0.0, min(2.0, float(value)))
        except ValueError:
            return 0.25

    @staticmethod
    def _retryable(status_code: int) -> bool:
        return status_code in JinaReader.retryable_statuses or status_code >= 500

    def _read_response_body(self, response: httpx.Response) -> str:
        content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
        if content_type and content_type not in {"text/markdown", "text/plain"}:
            raise JinaReaderError(
                f"Jina Reader returned unsupported content type: {content_type}",
                status_code=response.status_code,
            )
        chunks: list[bytes] = []
        size = 0
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > self.max_content_bytes:
                raise JinaReaderError("Jina Reader response exceeds configured size limit")
            chunks.append(chunk)
        encoding = response.encoding or "utf-8"
        body = b"".join(chunks).decode(encoding, errors="replace")
        if not _usable_text(body, "text/plain"):
            raise JinaReaderError("Jina Reader returned empty or unusable content")
        return body

    def read(self, target_url: str) -> FetchedPage:
        safe_target = assert_public_url(target_url)
        reader_url = self._reader_url(safe_target)
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "text/markdown",
            "User-Agent": USER_AGENT,
        }
        last_error: JinaReaderError | None = None
        for attempt in range(self.max_retries + 1):
            try:
                self.request_count += 1
                with self.client.stream("GET", reader_url, headers=headers) as response:
                    status = response.status_code
                    if status in self.non_retryable_statuses:
                        if status == 401:
                            message = "Jina Reader authentication failed"
                        elif status == 403:
                            message = "Jina Reader access was forbidden"
                        else:
                            message = "Jina Reader request was rejected"
                        raise JinaReaderError(message, status_code=status)
                    if status >= 400:
                        error = JinaReaderError(
                            "Jina Reader transient failure" if self._retryable(status) else "Jina Reader failed",
                            status_code=status,
                            retryable=self._retryable(status),
                        )
                        if error.retryable and attempt < self.max_retries:
                            self.sleep(self._retry_after_seconds(response))
                            last_error = error
                            continue
                        raise error
                    body = self._read_response_body(response)
                    return FetchedPage(
                        url=safe_target,
                        body=body,
                        status_code=status,
                        content_type="text/markdown",
                        fetch_provider="jina_reader",
                    )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                error = JinaReaderError("Jina Reader transport error", retryable=True)
                if attempt < self.max_retries:
                    self.sleep(0.25)
                    last_error = error
                    continue
                raise error from exc
        raise last_error or JinaReaderError("Jina Reader failed")


def jina_reader_from_settings(settings: Any) -> tuple[JinaReader | None, str | None]:
    return JinaReader.from_settings(settings)


def normalize_url(value: str) -> str:
    value = unescape(value.strip())
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", value) and not re.match(r"^https?:", value, re.I):
        raise UnsafeURLError("Only HTTP(S) URLs are allowed")
    if value.startswith("//"):
        value = "https:" + value
    if not re.match(r"^https?://", value, re.I):
        value = "https://" + value
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        raise UnsafeURLError("URL has no hostname")
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise UnsafeURLError("Only HTTP(S) URLs are allowed")
    if parsed.username or parsed.password:
        raise UnsafeURLError("URLs with credentials are not allowed")
    if parsed.port not in {None, 80, 443}:
        raise UnsafeURLError("Non-standard ports are not allowed")
    path = parsed.path or "/"
    return urlunparse((scheme, host + (f":{parsed.port}" if parsed.port else ""), path, "", parsed.query, ""))


def _is_forbidden_ip(value: str) -> bool:
    ip = ipaddress.ip_address(value)
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def assert_public_url(url: str) -> str:
    normalized = normalize_url(url)
    host = urlparse(normalized).hostname or ""
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        raise UnsafeURLError("Local hosts are not allowed")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if _is_forbidden_ip(host):
            raise UnsafeURLError("Private or reserved IPs are not allowed")
        return normalized
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)}
    except socket.gaierror as exc:
        raise UnsafeURLError(f"DNS lookup failed for {host}") from exc
    if not addresses or any(_is_forbidden_ip(address) for address in addresses):
        raise UnsafeURLError("Hostname resolves to a non-public IP")
    return normalized


def _profile_urls(account: Account) -> list[str]:
    urls: list[str] = []
    if account.url:
        urls.append(account.url)
    urls.extend(match.rstrip(".,;:!?") for match in URL_RE.findall(account.description or ""))
    entities = account.entities or {}
    for section in (entities.get("url") or {}, entities.get("description") or {}):
        for item in section.get("urls") or []:
            value = item.get("expanded_url") or item.get("unwound_url") or item.get("url")
            if value:
                urls.append(value)
    seen: set[str] = set()
    output: list[str] = []
    for value in urls:
        try:
            normalized = normalize_url(value)
        except (UnsafeURLError, ValueError):
            continue
        if normalized not in seen:
            seen.add(normalized)
            output.append(normalized)
    return output


def _evidence(text: str, needle: str, radius: int = 90) -> str:
    clean = re.sub(r"\s+", " ", text).strip()
    index = clean.lower().find(needle.lower())
    if index < 0:
        return clean[: radius * 2]
    return clean[max(0, index - radius) : index + len(needle) + radius]


def _usable_text(body: str, content_type: str) -> bool:
    if not body.strip():
        return False
    if content_type in {"text/html", "application/xhtml+xml"}:
        soup = BeautifulSoup(body, "html.parser")
        text = soup.get_text(" ", strip=True)
        if text:
            return True
        return bool(soup.find("a", href=True))
    return bool(re.search(r"[\w@\u4e00-\u9fff]", body))


def _iter_text_links(text: str) -> list[tuple[str, str]]:
    links: list[tuple[str, str]] = []
    seen: set[str] = set()
    for match in MARKDOWN_LINK_RE.finditer(text):
        label = match.group(1).strip()
        href = match.group(2).strip()
        if href not in seen:
            seen.add(href)
            links.append((href, label))
    for match in URL_RE.finditer(text):
        href = match.group(0).rstrip(".,;:!?")
        if href not in seen:
            seen.add(href)
            links.append((href, href))
    return links


def _contact_from_link(account_id: str, href: str, source_url: str, source_kind: str, text: str) -> ContactPoint | None:
    href = unescape(href.strip())
    if href.lower().startswith("mailto:"):
        email = href[7:].split("?", 1)[0].strip().lower()
        if EMAIL_RE.fullmatch(email):
            return ContactPoint(
                account_id=account_id,
                contact_type="email",
                value=email,
                url=f"mailto:{email}",
                source_url=source_url,
                source_kind=source_kind,
                evidence=_evidence(text, email),
            )
        return None
    try:
        normalized = normalize_url(href)
    except (UnsafeURLError, ValueError):
        return None
    parsed = urlparse(normalized)
    host = (parsed.hostname or "").lower()
    contact_type = SOCIAL_HOSTS.get(host)
    if not contact_type:
        return None
    return ContactPoint(
        account_id=account_id,
        contact_type=contact_type,  # type: ignore[arg-type]
        value=normalized,
        url=normalized,
        source_url=source_url,
        source_kind=source_kind,
        evidence=_evidence(text, href),
    )


class PublicContactCrawler:
    def __init__(
        self,
        *,
        max_pages: int = 5,
        timeout: float = 10,
        max_bytes: int = 2 * 1024 * 1024,
        cache_get: Callable[[str], dict[str, Any] | None] | None = None,
        cache_set: Callable[[str, int, str, str], None] | None = None,
        client: httpx.Client | None = None,
        jina_reader: JinaReader | None = None,
        jina_config_warning: str | None = None,
    ) -> None:
        self.max_pages = max(1, min(10, max_pages))
        self.max_bytes = max_bytes
        self.cache_get = cache_get
        self.cache_set = cache_set
        self.jina_reader = jina_reader
        self.jina_config_warning = jina_config_warning
        self.client = client or httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
            timeout=timeout,
            follow_redirects=False,
        )
        self._owns_client = client is None
        self._robots: dict[str, RobotFileParser] = {}
        self._jina_warnings: list[str] = []
        self._jina_config_warning_reported = False
        self.diagnostics: dict[str, Any] = {
            "jina_reader_enabled": bool(jina_reader or jina_config_warning),
            "jina_fallback_attempts": 0,
            "jina_http_requests": 0,
            "jina_reader_successes": 0,
            "jina_reader_failures": 0,
            "jina_reader_cache_hits": 0,
        }

    def close(self) -> None:
        if self._owns_client:
            self.client.close()
        if self.jina_reader:
            self.jina_reader.close()

    @staticmethod
    def _jina_allowed_for_url(url: str) -> bool:
        host = (urlparse(url).hostname or "").lower()
        return host not in X_HOSTS

    @staticmethod
    def _direct_status_allows_jina(status_code: int) -> bool:
        return status_code in {408, 429} or status_code >= 500

    def _fetch_direct(self, url: str) -> FetchedPage:
        current = url
        for _ in range(4):
            current = assert_public_url(current)
            with self.client.stream("GET", current) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        raise httpx.HTTPError("Redirect without Location")
                    current = urljoin(current, location)
                    continue
                content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                if content_type and content_type not in {"text/html", "application/xhtml+xml", "text/plain"}:
                    if not 200 <= response.status_code < 300:
                        return FetchedPage(current, "", response.status_code, content_type, "direct")
                    raise ValueError(f"Unsupported content type: {content_type}")
                chunks: list[bytes] = []
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise ValueError("Page exceeds 2 MB limit")
                    chunks.append(chunk)
                encoding = response.encoding or "utf-8"
                body = b"".join(chunks).decode(encoding, errors="replace")
                return FetchedPage(current, body, response.status_code, content_type, "direct")
        raise httpx.TooManyRedirects("More than 3 redirects")

    def _fetch_with_jina(self, url: str, reason: str) -> FetchedPage | None:
        if not self._jina_allowed_for_url(url):
            return None
        if self.jina_reader is None:
            if not self.jina_config_warning:
                return None
            self.diagnostics["jina_fallback_attempts"] += 1
            if self.jina_config_warning:
                self.diagnostics["jina_reader_failures"] += 1
                if not self._jina_config_warning_reported:
                    self._jina_warnings.append(
                        f"{url}: Jina Reader fallback unavailable: {self.jina_config_warning}"
                    )
                    self._jina_config_warning_reported = True
            return None
        self.diagnostics["jina_fallback_attempts"] += 1
        request_count_before = self.jina_reader.request_count
        try:
            page = self.jina_reader.read(url)
        except (JinaReaderError, ValueError) as exc:
            self.diagnostics["jina_http_requests"] += self.jina_reader.request_count - request_count_before
            self.diagnostics["jina_reader_failures"] += 1
            status = f" HTTP {exc.status_code}" if isinstance(exc, JinaReaderError) and exc.status_code else ""
            self._jina_warnings.append(
                f"{url}: Jina Reader fallback failed after {reason}:{status} {exc}"
            )
            return None
        self.diagnostics["jina_http_requests"] += self.jina_reader.request_count - request_count_before
        self.diagnostics["jina_reader_successes"] += 1
        if self.cache_set:
            self.cache_set(url, page.status_code, "text/markdown; fetch_provider=jina_reader", page.body)
        return page

    def _fetch_raw(self, url: str, *, check_robots: bool = True) -> tuple[str, str, int, str]:
        url = assert_public_url(url)
        cached = self.cache_get(url) if self.cache_get else None
        if cached:
            cached_from_jina = "jina_reader" in (cached.get("content_type") or "")
            if cached_from_jina and self.jina_reader is None and self.jina_config_warning is None:
                cached = None
            elif cached_from_jina:
                self.diagnostics["jina_reader_cache_hits"] += 1
                return url, cached.get("body", ""), int(cached.get("status_code", 200)), cached.get("content_type") or ""
            else:
                return url, cached.get("body", ""), int(cached.get("status_code", 200)), cached.get("content_type") or ""

        if check_robots and not self._allowed_by_robots(url):
            raise PermissionError("robots.txt disallows this page")

        try:
            page = self._fetch_direct(url)
        except UnsafeURLError:
            raise
        except ValueError as exc:
            if check_robots and "Unsupported content type" in str(exc):
                fallback = self._fetch_with_jina(url, str(exc))
                if fallback:
                    return fallback.url, fallback.body, fallback.status_code, fallback.content_type
            raise
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            if check_robots:
                fallback = self._fetch_with_jina(url, exc.__class__.__name__)
                if fallback:
                    return fallback.url, fallback.body, fallback.status_code, fallback.content_type
            raise

        reason: str | None = None
        if self._direct_status_allows_jina(page.status_code):
            reason = f"HTTP {page.status_code}"
        elif 200 <= page.status_code < 300 and not _usable_text(page.body, page.content_type):
            reason = "empty or unusable direct content"
        if reason and check_robots:
            fallback = self._fetch_with_jina(url, reason)
            if fallback:
                return fallback.url, fallback.body, fallback.status_code, fallback.content_type
        if self.cache_set:
            self.cache_set(page.url, page.status_code, page.content_type, page.body)
        return page.url, page.body, page.status_code, page.content_type

    def _allowed_by_robots(self, url: str) -> bool:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin in self._robots:
            return self._robots[origin].can_fetch(USER_AGENT, url)
        parser = RobotFileParser()
        robots_url = origin + "/robots.txt"
        try:
            _, body, status, _ = self._fetch_raw(robots_url, check_robots=False)
            if status < 400:
                parser.set_url(robots_url)
                parser.parse(body.splitlines())
            else:
                parser.parse([])
        except Exception:
            parser.parse([])
        self._robots[origin] = parser
        return parser.can_fetch(USER_AGENT, url)

    def crawl_account(self, account: Account) -> CrawlResult:
        result = CrawlResult()
        warning_start = len(self._jina_warnings)
        diagnostic_start = dict(self.diagnostics)
        seen_contacts: set[tuple[str, str, str]] = set()

        def add(contact: ContactPoint | None) -> None:
            if contact is None:
                return
            key = (contact.contact_type, contact.value.lower(), contact.source_url)
            if key not in seen_contacts:
                seen_contacts.add(key)
                result.contacts.append(contact)

        x_url = f"https://x.com/{account.username}"
        bio = account.description or ""
        for match in EMAIL_RE.finditer(bio):
            email = match.group(1).lower()
            add(ContactPoint(
                account_id=account.id,
                contact_type="email",
                value=email,
                url=f"mailto:{email}",
                source_url=x_url,
                source_kind="x_profile",
                evidence=_evidence(bio, email),
            ))

        starts = _profile_urls(account)
        crawl_starts: list[str] = []
        for url in starts:
            try:
                url = assert_public_url(url)
            except Exception as exc:
                result.warnings.append(f"{url}: {exc}")
                continue
            social = _contact_from_link(account.id, url, x_url, "x_profile", bio or url)
            add(social)
            if social is None:
                add(ContactPoint(
                    account_id=account.id,
                    contact_type="website",
                    value=url,
                    url=url,
                    source_url=x_url,
                    source_kind="x_profile",
                    evidence=_evidence(bio or url, url),
                ))
                crawl_starts.append(url)

        queue: deque[tuple[str, str, int]] = deque((url, "profile_link", 0) for url in crawl_starts)
        visited: set[str] = set()
        official_origins: set[str] = set()
        while queue and result.pages_fetched < self.max_pages:
            requested, source_kind, depth = queue.popleft()
            try:
                requested = normalize_url(requested)
            except (UnsafeURLError, ValueError):
                continue
            if requested in visited:
                continue
            visited.add(requested)
            try:
                final_url, body, status, content_type = self._fetch_raw(requested)
            except Exception as exc:
                result.warnings.append(f"{requested}: {exc}")
                continue
            result.pages_fetched += 1
            if status >= 400:
                result.warnings.append(f"{final_url}: HTTP {status}")
                continue

            parsed_final = urlparse(final_url)
            origin = f"{parsed_final.scheme}://{parsed_final.netloc}"
            if parsed_final.hostname not in LINK_AGGREGATORS:
                official_origins.add(origin)
            normalized_content_type = content_type.split(";", 1)[0].lower()
            jina_markdown = (
                "fetch_provider=jina_reader" in content_type
                or normalized_content_type == "text/markdown"
            )
            soup = BeautifulSoup(body, "html.parser") if not jina_markdown else None
            text = body if jina_markdown else soup.get_text(" ", strip=True)  # type: ignore[union-attr]
            for match in EMAIL_RE.finditer(text):
                email = match.group(1).lower()
                add(ContactPoint(
                    account_id=account.id,
                    contact_type="email",
                    value=email,
                    url=f"mailto:{email}",
                    source_url=final_url,
                    source_kind=source_kind,
                    evidence=_evidence(text, email),
                ))

            links = (
                _iter_text_links(body)
                if jina_markdown
                else [
                    (str(anchor.get("href")), anchor.get_text(" ", strip=True))
                    for anchor in soup.find_all("a", href=True)  # type: ignore[union-attr]
                ]
            )
            for href_value, label in links:
                href = urljoin(final_url, href_value)
                add(_contact_from_link(account.id, href, final_url, source_kind, f"{label} {text}"))
                try:
                    normalized = normalize_url(href)
                except (UnsafeURLError, ValueError):
                    continue
                parsed = urlparse(normalized)
                target_origin = f"{parsed.scheme}://{parsed.netloc}"
                same_origin = target_origin == origin
                from_aggregator = parsed_final.hostname in LINK_AGGREGATORS
                if CONTACT_PATH_RE.search(parsed.path) and (same_origin or target_origin in official_origins):
                    add(ContactPoint(
                        account_id=account.id,
                        contact_type="contact_page",
                        value=normalized,
                        url=normalized,
                        source_url=final_url,
                        source_kind=source_kind,
                        evidence=(label or normalized)[:280],
                    ))
                    if depth < 1:
                        queue.append((normalized, "official_site", depth + 1))
                elif depth < 1 and (same_origin or from_aggregator) and parsed.hostname not in SOCIAL_HOSTS:
                    if same_origin and CONTACT_PATH_RE.search(parsed.path):
                        queue.append((normalized, "official_site", depth + 1))
                    elif from_aggregator and parsed.hostname not in LINK_AGGREGATORS:
                        queue.append((normalized, "link_aggregator", depth + 1))
        result.warnings.extend(self._jina_warnings[warning_start:])
        result.diagnostics = {
            key: (
                value - diagnostic_start.get(key, 0)
                if (
                    isinstance(value, int)
                    and not isinstance(value, bool)
                    and isinstance(diagnostic_start.get(key), int)
                    and not isinstance(diagnostic_start.get(key), bool)
                )
                else value
            )
            for key, value in self.diagnostics.items()
        }
        return result
