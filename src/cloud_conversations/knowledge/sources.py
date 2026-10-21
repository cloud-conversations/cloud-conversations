"""Fetching a knowledge source's pages, and cleaning them to text."""

from __future__ import annotations

import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from bs4 import BeautifulSoup, Tag

from cloud_conversations.config import USER_AGENT
from cloud_conversations.knowledge.errors import FetchError
from cloud_conversations.schema.knowledge import (
    BlogSource,
    Document,
    Knowledge,
    Source,
    SphinxSource,
)

log = logging.getLogger(__name__)

_SPHINX_GENERATED = ("genindex", "search", "py-modindex")

_STRIPPED_TAGS = ["header", "footer", "nav", "aside", "script", "style"]

_STRIPPED_CLASSES = (
    "toc",
    "toctree-wrapper",
    "headerlink",
    "wy-nav-side",
    "rst-content-toc",
    "related-docs",
)


def fetch(name: str, source: Source, knowledge: Knowledge) -> list[Document]:
    """Every page of a source, cleaned to text.

    An unreadable page is skipped. Raises FetchError on an unreadable index.
    """
    at = datetime.now(timezone.utc)
    urls = (
        _sphinx_urls(source, knowledge)
        if isinstance(source, SphinxSource)
        else _blog_urls(source, knowledge)
    )
    documents = []
    for url in urls:
        document = _page(name, url, knowledge, at)
        if document is not None and len(document.content) >= source.min_chars:
            documents.append(document)
    return documents


def _sphinx_urls(source: SphinxSource, knowledge: Knowledge) -> list[str]:
    """Every documentation page linked from the site's index page."""
    index = urllib.parse.urljoin(source.url, source.index)
    excluded = {urllib.parse.urljoin(source.url, e) for e in source.exclude}
    soup = BeautifulSoup(_get(index, knowledge), "html.parser")

    urls: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        href = str(anchor["href"]).split("#")[0]
        if not href.endswith(".html") or any(g in href for g in _SPHINX_GENERATED):
            continue
        full = urllib.parse.urljoin(source.url, href)
        if full.startswith(source.url) and full not in excluded:
            urls.add(full)
    return sorted(urls)


def _blog_urls(source: BlogSource, knowledge: Knowledge) -> list[str]:
    """Every post on the listing pages in one of the source's categories."""
    wanted = set(source.categories)
    found: set[str] = set()
    page = 1
    while True:
        listing = source.url if page == 1 else f"{source.url}/page/{page}/"
        try:
            html = _get(listing, knowledge)
        except FetchError as exc:
            if exc.status != 404:
                log.warning("blog listing stopped at page %d: %s", page, exc)
            return sorted(found)

        soup = BeautifulSoup(html, "html.parser")
        for article in soup.find_all("article", class_="post-item"):
            url = _post_url(article, source.url, wanted)
            if url is not None:
                found.add(url)

        if not soup.find("a", string=lambda t: bool(t) and "next" in t.lower()):
            return sorted(found)
        page += 1


def _post_url(article: Tag, base: str, categories: set[str]) -> str | None:
    """A listing entry's post URL, if it is in one of the categories."""
    if categories and not categories & _categories_of(article):
        return None
    link = article.find("a", href=True)
    if not isinstance(link, Tag):
        return None
    href = str(link["href"])
    return href.rstrip("/") + "/" if href.startswith(base) else None


def _categories_of(article: Tag) -> set[str]:
    return {
        str(a["href"]).rstrip("/").rsplit("/", 1)[-1]
        for a in article.find_all("a", href=True)
        if "/categories/" in str(a["href"])
    }


def _page(
    name: str, url: str, knowledge: Knowledge, at: datetime
) -> Document | None:
    """One page as text, or None if it could not be read."""
    try:
        html = _get(url, knowledge)
    except FetchError as exc:
        log.warning("skipping %s: %s", url, exc)
        return None

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(_STRIPPED_TAGS):
        tag.decompose()
    root = _content_root(soup)
    for class_name in _STRIPPED_CLASSES:
        for tag in root.find_all(class_=class_name):
            tag.decompose()

    lines = (line.strip() for line in root.get_text(separator="\n").splitlines())
    return Document(
        source=name,
        url=url,
        title=_title(soup, root),
        content="\n".join(line for line in lines if line),
        fetched_at=at,
    )


def _content_root(soup: BeautifulSoup) -> Tag:
    """The element with the page's own content, or the whole document."""
    found = (
        soup.find("div", attrs={"role": "main"})
        or soup.find("div", class_="rst-content")
        or soup.find("article")
        or soup.find("main")
    )
    return found if isinstance(found, Tag) else soup


def _title(soup: BeautifulSoup, root: Tag) -> str:
    heading = root.find("h1")
    if heading:
        return heading.get_text(strip=True)
    title = soup.find("title")
    if not title:
        return ""
    return title.get_text(strip=True).split(" — ")[0].split(" | ")[0].strip()


def _get(url: str, knowledge: Knowledge) -> str:
    """Read a page, retrying a timeout or a server error."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    attempts = knowledge.fetch_attempts
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(
                request, timeout=knowledge.fetch_timeout_s
            ) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                body: bytes = response.read()
            return body.decode(charset, errors="replace")
        except urllib.error.HTTPError as exc:
            if exc.code < 500 or attempt == attempts:
                raise FetchError(f"{url}: HTTP {exc.code}", status=exc.code) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt == attempts:
                raise FetchError(f"{url}: {exc}") from exc
        time.sleep(knowledge.fetch_retry_s * attempt)
    raise FetchError(f"{url}: made no request")  # pragma: no cover
