from pathlib import Path

import httpx
import pytest

import kol_search.contacts as contacts_module
from kol_search.contacts import PublicContactCrawler, UnsafeURLError, assert_public_url, normalize_url
from kol_search.models import Account


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
