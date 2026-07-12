from __future__ import annotations

import ipaddress
import re
import socket
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
CONTACT_PATH_RE = re.compile(
    r"/(contact|about|media|press|partnership|partner|collab|business|联系|关于|合作|商务)", re.I
)
LINK_AGGREGATORS = {"linktr.ee", "beacons.ai", "bio.link", "linkin.bio", "solo.to", "allmylinks.com"}
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
        if _is_forbidden_ip(host):
            raise UnsafeURLError("Private or reserved IPs are not allowed")
        return normalized
    except ValueError:
        pass
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
    ) -> None:
        self.max_pages = max(1, min(10, max_pages))
        self.max_bytes = max_bytes
        self.cache_get = cache_get
        self.cache_set = cache_set
        self.client = client or httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
            timeout=timeout,
            follow_redirects=False,
        )
        self._owns_client = client is None
        self._robots: dict[str, RobotFileParser] = {}

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def _fetch_raw(self, url: str, *, check_robots: bool = True) -> tuple[str, str, int, str]:
        url = assert_public_url(url)
        cached = self.cache_get(url) if self.cache_get else None
        if cached:
            return url, cached.get("body", ""), int(cached.get("status_code", 200)), cached.get("content_type") or ""

        if check_robots and not self._allowed_by_robots(url):
            raise PermissionError("robots.txt disallows this page")

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
                if self.cache_set:
                    self.cache_set(current, response.status_code, content_type, body)
                return current, body, response.status_code, content_type
        raise httpx.TooManyRedirects("More than 3 redirects")

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
                final_url, body, status, _ = self._fetch_raw(requested)
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
            soup = BeautifulSoup(body, "html.parser")
            text = soup.get_text(" ", strip=True)
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

            for anchor in soup.find_all("a", href=True):
                href = urljoin(final_url, str(anchor.get("href")))
                label = anchor.get_text(" ", strip=True)
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
        return result
