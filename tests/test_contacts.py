from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

import kol_search.contacts as contacts_module
from kol_search.contacts import (
    JinaReader,
    PublicContactCrawler,
    UnsafeURLError,
    assert_public_url,
    jina_reader_from_settings,
    normalize_url,
)
from kol_search.models import Account
from kol_search.settings import Settings


def test_normalize_and_reject_private_urls():
    assert normalize_url("example.org/contact#team") == "https://example.org/contact"
    with pytest.raises(UnsafeURLError):
        assert_public_url("http://127.0.0.1/admin")
    with pytest.raises(UnsafeURLError):
        normalize_url("ftp://example.org/file")

    crawler = PublicContactCrawler(client=httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(500)), trust_env=False))
    result = crawler.crawl_account(Account(id="1", username="alice", url="http://127.0.0.1/private"))
    assert not result.contacts
    assert result.warnings


def test_profile_bio_social_link_is_contact_without_crawling(monkeypatch):
    monkeypatch.setattr(contacts_module, "assert_public_url", normalize_url)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"social profile should not be crawled: {request.url}")

    client = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
    crawler = PublicContactCrawler(client=client)
    result = crawler.crawl_account(Account(
        id="1", username="alice", description="Community: https://t.me/exampledao"
    ))
    assert [(contact.contact_type, contact.value) for contact in result.contacts] == [
        ("telegram", "https://t.me/exampledao")
    ]


def test_crawler_extracts_public_contacts_and_respects_robots(monkeypatch, tmp_path: Path):
    pages = {
        "https://example.org/robots.txt": "User-agent: *\nAllow: /\n",
        "https://example.org/": """
            <html><body>
              Partnerships: team@example.org
              <a href="/contact">Contact us</a>
              <a href="https://t.me/exampledao">Telegram</a>
            </body></html>
        """,
        "https://example.org/contact": '<a href="mailto:biz@example.org">Business email</a>',
    }

    def handler(request: httpx.Request) -> httpx.Response:
        body = pages.get(str(request.url), "not found")
        return httpx.Response(200 if str(request.url) in pages else 404, text=body, headers={"content-type": "text/html"})

    monkeypatch.setattr(contacts_module, "assert_public_url", normalize_url)
    client = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
    crawler = PublicContactCrawler(client=client, max_pages=5)
    account = Account(
        id="1",
        username="alice",
        description="DeFi researcher",
        url="https://example.org/",
    )
    result = crawler.crawl_account(account)
    values = {(contact.contact_type, contact.value) for contact in result.contacts}
    assert ("email", "team@example.org") in values
    assert ("email", "biz@example.org") in values
    assert ("telegram", "https://t.me/exampledao") in values
    assert any(contact.contact_type == "contact_page" for contact in result.contacts)
    assert result.pages_fetched == 2


def test_crawler_does_not_fetch_robots_disallowed_page(monkeypatch):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private", headers={"content-type": "text/plain"})
        return httpx.Response(200, text="secret@example.org", headers={"content-type": "text/html"})

    monkeypatch.setattr(contacts_module, "assert_public_url", normalize_url)
    client = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
    crawler = PublicContactCrawler(client=client)
    account = Account(id="1", username="alice", url="https://example.org/private")
    result = crawler.crawl_account(account)
    assert not result.contacts or all(contact.contact_type == "website" for contact in result.contacts)
    assert "https://example.org/private" not in seen
    assert any("robots.txt" in warning for warning in result.warnings)


def test_crawler_rejects_too_many_redirects_and_large_pages(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404, text="", headers={"content-type": "text/plain"})
        if request.url.path.startswith("/redirect"):
            number = int(request.url.path.removeprefix("/redirect") or "0")
            return httpx.Response(302, headers={"location": f"/redirect{number + 1}"})
        return httpx.Response(200, content=b"x" * 101, headers={"content-type": "text/html"})

    monkeypatch.setattr(contacts_module, "assert_public_url", normalize_url)
    client = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
    redirect_crawler = PublicContactCrawler(client=client)
    redirect_result = redirect_crawler.crawl_account(Account(id="1", username="a", url="https://example.org/redirect0"))
    assert any("redirect" in warning.lower() for warning in redirect_result.warnings)

    large_crawler = PublicContactCrawler(client=client, max_bytes=100)
    large_result = large_crawler.crawl_account(Account(id="2", username="b", url="https://example.org/large"))
    assert any("2 MB" in warning for warning in large_result.warnings)


def test_jina_reader_settings_are_disabled_by_default_and_lazy():
    settings = Settings(_env_file=None)
    assert settings.jina_reader_enabled is False
    reader, warning = jina_reader_from_settings(settings)
    assert reader is None
    assert warning is None

    enabled = Settings(_env_file=None, JINA_READER_ENABLED=True)
    reader, warning = jina_reader_from_settings(enabled)
    assert reader is None
    assert "JINA_API_KEY" in (warning or "")


def test_jina_reader_settings_reject_invalid_bounds():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, JINA_READER_TIMEOUT_SECONDS=0)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, JINA_READER_TIMEOUT_SECONDS=61)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, JINA_READER_MAX_RETRIES=-1)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, JINA_READER_MAX_RETRIES=4)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, JINA_READER_MAX_CONTENT_BYTES=0)


def test_jina_not_called_when_disabled_after_direct_failure(monkeypatch):
    monkeypatch.setattr(contacts_module, "assert_public_url", normalize_url)

    def direct_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404, text="", headers={"content-type": "text/plain"})
        return httpx.Response(500, text="server error", headers={"content-type": "text/html"})

    client = httpx.Client(transport=httpx.MockTransport(direct_handler), trust_env=False)
    crawler = PublicContactCrawler(client=client)
    result = crawler.crawl_account(Account(id="1", username="alice", url="https://example.org/"))
    assert result.diagnostics["jina_fallback_attempts"] == 0
    assert all(contact.contact_type == "website" for contact in result.contacts)
    assert any("HTTP 500" in warning for warning in result.warnings)


def test_jina_not_called_when_direct_fetch_succeeds(monkeypatch):
    monkeypatch.setattr(contacts_module, "assert_public_url", normalize_url)

    def direct_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404, text="", headers={"content-type": "text/plain"})
        return httpx.Response(
            200,
            text="<html><body>Reach us at team@example.org</body></html>",
            headers={"content-type": "text/html"},
        )

    def jina_handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"Jina should not be called: {request.url}")

    direct = httpx.Client(transport=httpx.MockTransport(direct_handler), trust_env=False)
    jina = JinaReader(
        api_key="test-key",
        base_url="https://reader.test",
        client=httpx.Client(transport=httpx.MockTransport(jina_handler), trust_env=False),
    )
    crawler = PublicContactCrawler(client=direct, jina_reader=jina)
    result = crawler.crawl_account(Account(id="1", username="alice", url="https://example.org/"))
    values = {(contact.contact_type, contact.value) for contact in result.contacts}
    assert ("email", "team@example.org") in values
    assert result.diagnostics["jina_fallback_attempts"] == 0


def test_direct_text_plain_does_not_gain_markdown_link_traversal(monkeypatch):
    monkeypatch.setattr(contacts_module, "assert_public_url", normalize_url)
    seen: list[str] = []

    def direct_handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(404, text="", headers={"content-type": "text/plain"})
        if request.url.path == "/contact":
            return httpx.Response(
                200,
                text="<html><body>linked@example.org</body></html>",
                headers={"content-type": "text/html"},
            )
        return httpx.Response(
            200,
            text=(
                "Plain contact plain@example.org "
                "[Contact](https://example.org/contact) "
                "https://t.me/plain"
            ),
            headers={"content-type": "text/plain"},
        )

    client = httpx.Client(transport=httpx.MockTransport(direct_handler), trust_env=False)
    crawler = PublicContactCrawler(client=client, max_pages=5)
    result = crawler.crawl_account(Account(id="1", username="alice", url="https://example.org/"))
    values = {(contact.contact_type, contact.value) for contact in result.contacts}
    assert ("email", "plain@example.org") in values
    assert ("email", "linked@example.org") not in values
    assert not any(contact.contact_type == "telegram" for contact in result.contacts)
    assert not any(contact.contact_type == "contact_page" for contact in result.contacts)
    assert "https://example.org/contact" not in seen


def test_jina_fallback_uses_auth_and_original_url_for_markdown(monkeypatch):
    monkeypatch.setattr(contacts_module, "assert_public_url", normalize_url)
    jina_requests: list[httpx.Request] = []

    def direct_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404, text="", headers={"content-type": "text/plain"})
        if request.url.path == "/contact":
            return httpx.Response(
                200,
                text='<a href="mailto:biz@example.org">Business</a>',
                headers={"content-type": "text/html"},
            )
        return httpx.Response(500, text="server error", headers={"content-type": "text/html"})

    def jina_handler(request: httpx.Request) -> httpx.Response:
        jina_requests.append(request)
        assert request.headers["Authorization"] == "Bearer secret-test-key"
        assert request.headers["Accept"] == "text/markdown"
        assert "secret-test-key" not in str(request.url)
        assert str(request.url).startswith("https://reader.test/https://example.org/")
        return httpx.Response(
            200,
            text=(
                "Reach reader@example.org "
                "[Telegram](https://t.me/exampledao) "
                "[Contact](https://example.org/contact)"
            ),
            headers={"content-type": "text/markdown"},
        )

    direct = httpx.Client(transport=httpx.MockTransport(direct_handler), trust_env=False)
    jina = JinaReader(
        api_key="secret-test-key",
        base_url="https://reader.test",
        client=httpx.Client(transport=httpx.MockTransport(jina_handler), trust_env=False),
    )
    crawler = PublicContactCrawler(client=direct, jina_reader=jina, max_pages=3)
    result = crawler.crawl_account(Account(id="1", username="alice", url="https://example.org/"))
    values = {(contact.contact_type, contact.value) for contact in result.contacts}
    assert ("email", "reader@example.org") in values
    assert ("email", "biz@example.org") in values
    assert ("telegram", "https://t.me/exampledao") in values
    assert any(contact.contact_type == "contact_page" for contact in result.contacts)
    assert all("reader.test" not in contact.source_url for contact in result.contacts)
    assert any(contact.source_url == "https://example.org/" for contact in result.contacts)
    assert all(contact.status == "pending" for contact in result.contacts)
    assert len(jina_requests) == 1
    assert result.diagnostics["jina_fallback_attempts"] == 1
    assert result.diagnostics["jina_http_requests"] == 1
    assert result.diagnostics["jina_reader_successes"] == 1


def test_jina_enabled_without_key_reports_one_warning_across_eligible_pages(monkeypatch):
    monkeypatch.setattr(contacts_module, "assert_public_url", normalize_url)
    settings = Settings(_env_file=None, JINA_READER_ENABLED=True)
    reader, warning = jina_reader_from_settings(settings)
    assert reader is None
    assert warning

    def direct_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404, text="", headers={"content-type": "text/plain"})
        return httpx.Response(500, text="server error", headers={"content-type": "text/html"})

    client = httpx.Client(transport=httpx.MockTransport(direct_handler), trust_env=False)
    crawler = PublicContactCrawler(client=client, jina_reader=reader, jina_config_warning=warning, max_pages=5)
    result = crawler.crawl_account(Account(
        id="1",
        username="alice",
        url="https://example.org/",
        description="https://example.net/",
    ))
    missing_key_warnings = [item for item in result.warnings if "JINA_API_KEY" in item]
    assert len(missing_key_warnings) == 1
    assert result.diagnostics["jina_fallback_attempts"] == 2
    assert result.diagnostics["jina_http_requests"] == 0
    assert result.diagnostics["jina_reader_failures"] == 2


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/admin",
        "http://localhost/admin",
        "http://printer.local/admin",
        "http://169.254.169.254/latest/meta-data",
        "ftp://example.org/file",
        "https://example.org:444/admin",
        "https://user:pass@example.org/admin",
    ],
)
def test_unsafe_targets_are_rejected_before_jina(url: str):
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"no HTTP request should be made for unsafe URL: {request.url}")

    client = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
    jina = JinaReader(
        api_key="test-key",
        client=httpx.Client(transport=httpx.MockTransport(handler), trust_env=False),
    )
    crawler = PublicContactCrawler(client=client, jina_reader=jina)
    result = crawler.crawl_account(Account(id="1", username="alice", url=url))
    assert not result.contacts
    assert result.diagnostics["jina_fallback_attempts"] == 0


def test_robots_disallowed_page_is_not_sent_to_jina(monkeypatch):
    monkeypatch.setattr(contacts_module, "assert_public_url", normalize_url)
    seen: list[str] = []

    def direct_handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(
                200,
                text="User-agent: *\nDisallow: /private",
                headers={"content-type": "text/plain"},
            )
        return httpx.Response(200, text="secret@example.org", headers={"content-type": "text/html"})

    def jina_handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"robots-disallowed target went to Jina: {request.url}")

    direct = httpx.Client(transport=httpx.MockTransport(direct_handler), trust_env=False)
    jina = JinaReader(
        api_key="test-key",
        client=httpx.Client(transport=httpx.MockTransport(jina_handler), trust_env=False),
    )
    crawler = PublicContactCrawler(client=direct, jina_reader=jina)
    result = crawler.crawl_account(Account(id="1", username="alice", url="https://example.org/private"))
    assert "https://example.org/private" not in seen
    assert not any(contact.value == "secret@example.org" for contact in result.contacts)
    assert result.diagnostics["jina_fallback_attempts"] == 0


def test_unsafe_redirect_target_does_not_trigger_jina(monkeypatch):
    monkeypatch.setattr(
        contacts_module.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(None, None, None, None, ("93.184.216.34", 0))],
    )

    def direct_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404, text="", headers={"content-type": "text/plain"})
        return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})

    def jina_handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unsafe redirect target should not use Jina: {request.url}")

    direct = httpx.Client(transport=httpx.MockTransport(direct_handler), trust_env=False)
    jina = JinaReader(
        api_key="test-key",
        client=httpx.Client(transport=httpx.MockTransport(jina_handler), trust_env=False),
    )
    crawler = PublicContactCrawler(client=direct, jina_reader=jina)
    result = crawler.crawl_account(Account(id="1", username="alice", url="https://example.org/redirect"))
    assert any("Private or reserved IP" in warning for warning in result.warnings)
    assert result.diagnostics["jina_fallback_attempts"] == 0


@pytest.mark.parametrize("status", [401, 403])
def test_direct_auth_errors_do_not_trigger_jina(monkeypatch, status: int):
    monkeypatch.setattr(contacts_module, "assert_public_url", normalize_url)

    def direct_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404, text="", headers={"content-type": "text/plain"})
        return httpx.Response(status, text="access denied", headers={"content-type": "text/html"})

    def jina_handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"direct auth error should not use Jina: {request.url}")

    direct = httpx.Client(transport=httpx.MockTransport(direct_handler), trust_env=False)
    jina = JinaReader(
        api_key="test-key",
        client=httpx.Client(transport=httpx.MockTransport(jina_handler), trust_env=False),
    )
    crawler = PublicContactCrawler(client=direct, jina_reader=jina)
    result = crawler.crawl_account(Account(id="1", username="alice", url="https://example.org/private"))
    assert any(f"HTTP {status}" in warning for warning in result.warnings)
    assert result.diagnostics["jina_fallback_attempts"] == 0


@pytest.mark.parametrize(
    ("status", "content_type"),
    [
        (401, "application/json"),
        (403, "application/json"),
        (404, "application/octet-stream"),
    ],
)
def test_direct_nonfallback_status_with_unsupported_content_type_does_not_trigger_jina(
    monkeypatch,
    status: int,
    content_type: str,
):
    monkeypatch.setattr(contacts_module, "assert_public_url", normalize_url)
    jina_requests: list[httpx.Request] = []

    def direct_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404, text="", headers={"content-type": "text/plain"})
        return httpx.Response(status, content=b"{}", headers={"content-type": content_type})

    def jina_handler(request: httpx.Request) -> httpx.Response:
        jina_requests.append(request)
        return httpx.Response(200, text="bypass@example.org", headers={"content-type": "text/markdown"})

    direct = httpx.Client(transport=httpx.MockTransport(direct_handler), trust_env=False)
    jina = JinaReader(
        api_key="test-key",
        client=httpx.Client(transport=httpx.MockTransport(jina_handler), trust_env=False),
    )
    crawler = PublicContactCrawler(client=direct, jina_reader=jina)
    result = crawler.crawl_account(Account(id="1", username="alice", url="https://example.org/private"))
    assert jina_requests == []
    assert all(contact.contact_type == "website" for contact in result.contacts)
    assert any(f"HTTP {status}" in warning for warning in result.warnings)
    assert result.diagnostics["jina_fallback_attempts"] == 0
    assert result.diagnostics["jina_http_requests"] == 0


def test_login_page_content_does_not_trigger_jina(monkeypatch):
    monkeypatch.setattr(contacts_module, "assert_public_url", normalize_url)

    def direct_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404, text="", headers={"content-type": "text/plain"})
        return httpx.Response(
            200,
            text="<html><body>Please log in to continue</body></html>",
            headers={"content-type": "text/html"},
        )

    def jina_handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"login page should not use Jina: {request.url}")

    direct = httpx.Client(transport=httpx.MockTransport(direct_handler), trust_env=False)
    jina = JinaReader(
        api_key="test-key",
        client=httpx.Client(transport=httpx.MockTransport(jina_handler), trust_env=False),
    )
    crawler = PublicContactCrawler(client=direct, jina_reader=jina)
    result = crawler.crawl_account(Account(id="1", username="alice", url="https://example.org/private"))
    assert all(contact.contact_type == "website" for contact in result.contacts)
    assert result.diagnostics["jina_fallback_attempts"] == 0


def test_unsupported_direct_content_type_can_use_jina_fallback(monkeypatch):
    monkeypatch.setattr(contacts_module, "assert_public_url", normalize_url)

    def direct_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404, text="", headers={"content-type": "text/plain"})
        return httpx.Response(200, content=b"%PDF", headers={"content-type": "application/pdf"})

    def jina_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text="PDF contact: pdf@example.org",
            headers={"content-type": "text/markdown"},
        )

    direct = httpx.Client(transport=httpx.MockTransport(direct_handler), trust_env=False)
    jina = JinaReader(
        api_key="test-key",
        client=httpx.Client(transport=httpx.MockTransport(jina_handler), trust_env=False),
    )
    crawler = PublicContactCrawler(client=direct, jina_reader=jina)
    result = crawler.crawl_account(Account(id="1", username="alice", url="https://example.org/media-kit"))
    assert any(contact.value == "pdf@example.org" for contact in result.contacts)
    assert result.diagnostics["jina_fallback_attempts"] == 1
    assert result.diagnostics["jina_http_requests"] == 1


def test_jina_timeout_and_transport_errors_are_non_fatal(monkeypatch):
    monkeypatch.setattr(contacts_module, "assert_public_url", normalize_url)

    def direct_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404, text="", headers={"content-type": "text/plain"})
        return httpx.Response(500, text="server error", headers={"content-type": "text/html"})

    def jina_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow reader")

    direct = httpx.Client(transport=httpx.MockTransport(direct_handler), trust_env=False)
    jina = JinaReader(
        api_key="test-key",
        max_retries=0,
        client=httpx.Client(transport=httpx.MockTransport(jina_handler), trust_env=False),
    )
    crawler = PublicContactCrawler(client=direct, jina_reader=jina)
    result = crawler.crawl_account(Account(id="1", username="alice", url="https://example.org/"))
    assert result.diagnostics["jina_reader_failures"] == 1
    assert any("Jina Reader fallback failed" in warning for warning in result.warnings)


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_jina_non_retryable_statuses_are_not_retried(monkeypatch, status: int):
    monkeypatch.setattr(contacts_module, "assert_public_url", normalize_url)
    calls = 0

    def direct_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404, text="", headers={"content-type": "text/plain"})
        return httpx.Response(500, text="server error", headers={"content-type": "text/html"})

    def jina_handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status, text="reader error", headers={"content-type": "text/plain"})

    direct = httpx.Client(transport=httpx.MockTransport(direct_handler), trust_env=False)
    jina = JinaReader(
        api_key="secret-test-key",
        max_retries=2,
        client=httpx.Client(transport=httpx.MockTransport(jina_handler), trust_env=False),
        sleep=lambda _: None,
    )
    crawler = PublicContactCrawler(client=direct, jina_reader=jina)
    result = crawler.crawl_account(Account(id="1", username="alice", url="https://example.org/"))
    assert calls == 1
    assert result.diagnostics["jina_fallback_attempts"] == 1
    assert result.diagnostics["jina_http_requests"] == 1
    assert result.diagnostics["jina_reader_failures"] == 1
    assert "secret-test-key" not in "\n".join(result.warnings)
    if status == 401:
        assert any("authentication failed" in warning for warning in result.warnings)


@pytest.mark.parametrize("status", [408, 429, 500])
def test_jina_retryable_statuses_retry_within_bound(monkeypatch, status: int):
    monkeypatch.setattr(contacts_module, "assert_public_url", normalize_url)
    calls = 0

    def direct_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404, text="", headers={"content-type": "text/plain"})
        return httpx.Response(500, text="server error", headers={"content-type": "text/html"})

    def jina_handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                status,
                text="retry later",
                headers={"content-type": "text/plain", "retry-after": "99"},
            )
        return httpx.Response(
            200,
            text="retry@example.org",
            headers={"content-type": "text/markdown"},
        )

    direct = httpx.Client(transport=httpx.MockTransport(direct_handler), trust_env=False)
    jina = JinaReader(
        api_key="test-key",
        max_retries=1,
        client=httpx.Client(transport=httpx.MockTransport(jina_handler), trust_env=False),
        sleep=lambda _: None,
    )
    crawler = PublicContactCrawler(client=direct, jina_reader=jina)
    result = crawler.crawl_account(Account(id="1", username="alice", url="https://example.org/"))
    assert calls == 2
    assert result.diagnostics["jina_fallback_attempts"] == 1
    assert result.diagnostics["jina_http_requests"] == 2
    assert any(contact.value == "retry@example.org" for contact in result.contacts)


def test_jina_retry_count_never_exceeds_configuration(monkeypatch):
    monkeypatch.setattr(contacts_module, "assert_public_url", normalize_url)
    calls = 0

    def direct_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404, text="", headers={"content-type": "text/plain"})
        return httpx.Response(500, text="server error", headers={"content-type": "text/html"})

    def jina_handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500, text="reader down", headers={"content-type": "text/plain"})

    direct = httpx.Client(transport=httpx.MockTransport(direct_handler), trust_env=False)
    jina = JinaReader(
        api_key="test-key",
        max_retries=1,
        client=httpx.Client(transport=httpx.MockTransport(jina_handler), trust_env=False),
        sleep=lambda _: None,
    )
    crawler = PublicContactCrawler(client=direct, jina_reader=jina)
    result = crawler.crawl_account(Account(id="1", username="alice", url="https://example.org/"))
    assert calls == 2
    assert result.diagnostics["jina_fallback_attempts"] == 1
    assert result.diagnostics["jina_http_requests"] == 2
    assert result.diagnostics["jina_reader_failures"] == 1


@pytest.mark.parametrize(
    ("body", "content_type", "max_bytes", "expected"),
    [
        ("", "text/markdown", 100, "empty or unusable"),
        ("x" * 11, "text/markdown", 10, "size limit"),
        (".....", "text/markdown", 100, "empty or unusable"),
        ("team@example.org", "application/json", 100, "unsupported content type"),
    ],
)
def test_jina_rejects_bad_responses(monkeypatch, body: str, content_type: str, max_bytes: int, expected: str):
    monkeypatch.setattr(contacts_module, "assert_public_url", normalize_url)

    def direct_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404, text="", headers={"content-type": "text/plain"})
        return httpx.Response(500, text="server error", headers={"content-type": "text/html"})

    def jina_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body, headers={"content-type": content_type})

    direct = httpx.Client(transport=httpx.MockTransport(direct_handler), trust_env=False)
    jina = JinaReader(
        api_key="test-key",
        max_retries=0,
        max_content_bytes=max_bytes,
        client=httpx.Client(transport=httpx.MockTransport(jina_handler), trust_env=False),
    )
    crawler = PublicContactCrawler(client=direct, jina_reader=jina)
    result = crawler.crawl_account(Account(id="1", username="alice", url="https://example.org/"))
    assert any(expected in warning for warning in result.warnings)
    assert result.diagnostics["jina_reader_failures"] == 1


def test_jina_success_uses_existing_crawl_cache_under_original_url(monkeypatch):
    monkeypatch.setattr(contacts_module, "assert_public_url", normalize_url)
    cache: dict[str, dict] = {}
    direct_calls = 0
    jina_calls = 0

    def cache_get(url: str) -> dict | None:
        return cache.get(url)

    def cache_set(url: str, status_code: int, content_type: str, body: str) -> None:
        cache[url] = {
            "url": url,
            "status_code": status_code,
            "content_type": content_type,
            "body": body,
        }

    def direct_handler(request: httpx.Request) -> httpx.Response:
        nonlocal direct_calls
        direct_calls += 1
        if request.url.path == "/robots.txt":
            return httpx.Response(404, text="", headers={"content-type": "text/plain"})
        return httpx.Response(500, text="server error", headers={"content-type": "text/html"})

    def jina_handler(request: httpx.Request) -> httpx.Response:
        nonlocal jina_calls
        jina_calls += 1
        return httpx.Response(
            200,
            text="cached@example.org",
            headers={"content-type": "text/markdown"},
        )

    direct = httpx.Client(transport=httpx.MockTransport(direct_handler), trust_env=False)
    jina = JinaReader(
        api_key="test-key",
        client=httpx.Client(transport=httpx.MockTransport(jina_handler), trust_env=False),
    )
    crawler = PublicContactCrawler(
        client=direct,
        jina_reader=jina,
        cache_get=cache_get,
        cache_set=cache_set,
    )
    account = Account(id="1", username="alice", url="https://example.org/")
    first = crawler.crawl_account(account)
    second = crawler.crawl_account(account)
    assert any(contact.value == "cached@example.org" for contact in first.contacts)
    assert any(contact.value == "cached@example.org" for contact in second.contacts)
    assert "https://example.org/" in cache
    assert "jina_reader" in cache["https://example.org/"]["content_type"]
    assert "r.jina.ai" not in cache
    assert jina_calls == 1
    assert direct_calls == 2  # first robots.txt and first direct page only
    assert second.diagnostics["jina_reader_cache_hits"] == 1


def test_jina_cache_entry_is_ignored_when_jina_disabled(monkeypatch):
    monkeypatch.setattr(contacts_module, "assert_public_url", normalize_url)
    cache = {
        "https://example.org/": {
            "url": "https://example.org/",
            "status_code": 200,
            "content_type": "text/markdown; fetch_provider=jina_reader",
            "body": "cached@example.org",
        }
    }
    seen: list[str] = []

    def cache_get(url: str) -> dict | None:
        return cache.get(url)

    def direct_handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(404, text="", headers={"content-type": "text/plain"})
        return httpx.Response(
            200,
            text="<html><body>direct@example.org</body></html>",
            headers={"content-type": "text/html"},
        )

    direct = httpx.Client(transport=httpx.MockTransport(direct_handler), trust_env=False)
    crawler = PublicContactCrawler(client=direct, cache_get=cache_get)
    result = crawler.crawl_account(Account(id="1", username="alice", url="https://example.org/"))
    values = {(contact.contact_type, contact.value) for contact in result.contacts}
    assert ("email", "direct@example.org") in values
    assert ("email", "cached@example.org") not in values
    assert "https://example.org/" in seen
    assert result.diagnostics["jina_fallback_attempts"] == 0
    assert result.diagnostics["jina_reader_cache_hits"] == 0


def test_direct_cache_entry_is_used_when_jina_disabled(monkeypatch):
    monkeypatch.setattr(contacts_module, "assert_public_url", normalize_url)
    cache = {
        "https://example.org/": {
            "url": "https://example.org/",
            "status_code": 200,
            "content_type": "text/html",
            "body": "<html><body>directcache@example.org</body></html>",
        }
    }

    def cache_get(url: str) -> dict | None:
        return cache.get(url)

    def direct_handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"direct cache hit should avoid HTTP fetch: {request.url}")

    direct = httpx.Client(transport=httpx.MockTransport(direct_handler), trust_env=False)
    crawler = PublicContactCrawler(client=direct, cache_get=cache_get)
    result = crawler.crawl_account(Account(id="1", username="alice", url="https://example.org/"))
    assert any(contact.value == "directcache@example.org" for contact in result.contacts)
    assert result.diagnostics["jina_fallback_attempts"] == 0
    assert result.diagnostics["jina_reader_cache_hits"] == 0
