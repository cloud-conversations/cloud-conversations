"""Fetching, cleaning, chunking and caching, against pages served over loopback."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cloud_conversations.knowledge import chunks, index
from cloud_conversations.knowledge.errors import KnowledgeError
from cloud_conversations.knowledge.sources import fetch
from cloud_conversations.schema.knowledge import BlogSource, Knowledge, SphinxSource
from tests.conftest import FakeEmbedder
from tests.loopback import ORIGIN, serving_pages

PAGE = """\
<html><head><title>Leases — Chameleon Docs</title></head>
<body>
<nav>Site navigation</nav>
<div role="main">
  <h1>Leases</h1>
  <p>Reserve a bare-metal node with a lease.</p>
  <div class="toc">Table of contents</div>
</div>
<footer>Copyright</footer>
</body></html>
"""


def _contents(*hrefs: str) -> str:
    links = "".join(f'<a href="{h}">{h}</a>' for h in hrefs)
    return f"<html><body>{links}</body></html>"


def _page(title: str, body: str) -> str:
    return (
        f"<html><head><title>{title}</title></head><body>"
        f'<div role="main"><h1>{title}</h1><p>{body}</p></div>'
        "</body></html>"
    )


def _listing(posts: list[tuple[str, str]], next_page: str | None) -> str:
    articles = "".join(
        f'<article class="post-item"><a href="{ORIGIN}{path}">Post</a>'
        f'<a href="{ORIGIN}/categories/{category}/">{category}</a></article>'
        for path, category in posts
    )
    after = f'<a href="{ORIGIN}{next_page}">Next page</a>' if next_page else ""
    return f"<html><body>{articles}{after}</body></html>"


@pytest.fixture()
def knowledge(tmp_path: Path) -> Knowledge:
    return Knowledge(
        fetch_attempts=1, fetch_retry_s=0, cache_dir=str(tmp_path / "cache")
    )


def _sphinx(base: str, **fields) -> SphinxSource:
    return SphinxSource(kind="sphinx", url=f"{base}/docs/", label="Docs", **fields)


def _blog(base: str, **fields) -> BlogSource:
    return BlogSource(kind="blog", url=base, label="Blog", **fields)


def test_a_sphinx_source_reads_every_page_its_index_links(knowledge):
    pages = {
        "/docs/contents.html": _contents("leases.html", "images.html", "genindex.html"),
        "/docs/leases.html": PAGE,
        "/docs/images.html": _page("Images", "Boot an image."),
    }
    with serving_pages(pages) as (base, asked):
        documents = fetch("readthedocs", _sphinx(base), knowledge)

    assert [d.url for d in documents] == [
        f"{base}/docs/images.html",
        f"{base}/docs/leases.html",
    ]
    assert "/docs/genindex.html" not in asked


def test_a_sphinx_page_keeps_its_main_content_and_drops_the_furniture(knowledge):
    pages = {
        "/docs/contents.html": _contents("leases.html"),
        "/docs/leases.html": PAGE,
    }
    with serving_pages(pages) as (base, _):
        documents = fetch("readthedocs", _sphinx(base), knowledge)

    assert documents[0].title == "Leases"
    assert documents[0].content == "Leases\nReserve a bare-metal node with a lease."


def test_a_sphinx_source_ignores_a_link_outside_its_own_base(knowledge):
    pages = {
        "/docs/contents.html": _contents("leases.html", "../other/elsewhere.html"),
        "/docs/leases.html": PAGE,
        "/other/elsewhere.html": _page("Elsewhere", "Not these docs."),
    }
    with serving_pages(pages) as (base, _):
        documents = fetch("readthedocs", _sphinx(base), knowledge)

    assert [d.url for d in documents] == [f"{base}/docs/leases.html"]


def test_a_sphinx_source_excludes_the_pages_it_names(knowledge):
    pages = {
        "/docs/contents.html": _contents("index.html", "leases.html"),
        "/docs/index.html": _page("Welcome", "Everything, briefly."),
        "/docs/leases.html": PAGE,
    }
    with serving_pages(pages) as (base, _):
        documents = fetch(
            "readthedocs", _sphinx(base, exclude=["index.html"]), knowledge
        )

    assert [d.url for d in documents] == [f"{base}/docs/leases.html"]


def test_a_page_shorter_than_min_chars_is_dropped(knowledge):
    pages = {
        "/docs/contents.html": _contents("leases.html", "stub.html"),
        "/docs/leases.html": PAGE,
        "/docs/stub.html": _page("Stub", "See below."),
    }
    with serving_pages(pages) as (base, _):
        documents = fetch("readthedocs", _sphinx(base, min_chars=40), knowledge)

    assert [d.title for d in documents] == ["Leases"]


def test_a_page_that_cannot_be_read_is_skipped(knowledge):
    pages = {
        "/docs/contents.html": _contents("leases.html", "broken.html"),
        "/docs/leases.html": PAGE,
    }
    with serving_pages(pages, failing={"/docs/broken.html": 500}) as (base, _):
        documents = fetch("readthedocs", _sphinx(base), knowledge)

    assert [d.title for d in documents] == ["Leases"]


def test_an_index_that_cannot_be_read_fails_the_source(knowledge):
    with (
        serving_pages({}, failing={"/docs/contents.html": 503}) as (base, _),
        pytest.raises(KnowledgeError, match="HTTP 503"),
    ):
        fetch("readthedocs", _sphinx(base), knowledge)


def test_a_server_error_is_retried_and_a_missing_page_is_not(tmp_path):
    knowledge = Knowledge(
        fetch_attempts=3, fetch_retry_s=0, cache_dir=str(tmp_path / "cache")
    )
    with serving_pages(
        {"/docs/contents.html": _contents("gone.html", "broken.html")},
        failing={"/docs/gone.html": 404, "/docs/broken.html": 500},
    ) as (base, asked):
        assert fetch("readthedocs", _sphinx(base), knowledge) == []

    assert asked.count("/docs/gone.html") == 1
    assert asked.count("/docs/broken.html") == 3


def test_a_blog_source_keeps_only_the_categories_it_names(knowledge):
    pages = {
        "/": _listing(
            [("/tips/one/", "tips-and-tricks"), ("/news/two/", "announcements")], None
        ),
        "/tips/one/": _page("One", "A useful trick."),
        "/news/two/": _page("Two", "We have new hardware."),
    }
    with serving_pages(pages) as (base, _):
        documents = fetch(
            "blog", _blog(base, categories=["tips-and-tricks"]), knowledge
        )

    assert [d.url for d in documents] == [f"{base}/tips/one/"]


def test_a_blog_source_follows_the_next_link(knowledge):
    pages = {
        "/": _listing([("/tips/one/", "tips-and-tricks")], "/page/2/"),
        "/page/2/": _listing([("/tips/two/", "tips-and-tricks")], None),
        "/tips/one/": _page("One", "A useful trick."),
        "/tips/two/": _page("Two", "Another useful trick."),
    }
    with serving_pages(pages) as (base, _):
        documents = fetch(
            "blog", _blog(base, categories=["tips-and-tricks"]), knowledge
        )

    assert [d.title for d in documents] == ["One", "Two"]


def test_a_blog_source_stops_where_the_listing_runs_out(knowledge):
    pages = {
        "/": _listing([("/tips/one/", "tips-and-tricks")], "/page/2/"),
        "/tips/one/": _page("One", "A useful trick."),
    }
    with serving_pages(pages, failing={"/page/2/": 404}) as (base, _):
        documents = fetch(
            "blog", _blog(base, categories=["tips-and-tricks"]), knowledge
        )

    assert [d.title for d in documents] == ["One"]


def test_each_child_chunk_carries_its_source_and_page_title(knowledge):
    pages = {
        "/docs/contents.html": _contents("leases.html"),
        "/docs/leases.html": PAGE,
    }
    with serving_pages(pages) as (base, _):
        documents = chunks.split(
            fetch("readthedocs", _sphinx(base), knowledge), knowledge
        )

    children = [c for p in documents[0].parents for c in p.chunks]
    assert children
    assert all(c.content.startswith("[readthedocs: Leases]\n") for c in children)


def test_a_page_splits_into_parents_no_larger_than_configured(knowledge):
    small = Knowledge(parent_chars=120, parent_overlap=0, child_chars=40,
                      child_overlap=0)
    pages = {
        "/docs/contents.html": _contents("leases.html"),
        "/docs/leases.html": _page("Leases", " ".join(["reserve a node"] * 40)),
    }
    with serving_pages(pages) as (base, _):
        documents = chunks.split(fetch("readthedocs", _sphinx(base), knowledge), small)

    parents = documents[0].parents
    assert len(parents) > 1
    assert max(len(p.content) for p in parents) <= 120
    assert all(p.chunks for p in parents)


def test_a_fetch_is_cached_and_read_back_without_asking_again(knowledge):
    pages = {
        "/docs/contents.html": _contents("leases.html"),
        "/docs/leases.html": PAGE,
    }
    with serving_pages(pages) as (base, asked):
        source = _sphinx(base)
        fetched = index.collect("readthedocs", source, knowledge, refresh=True)
        requests = len(asked)
        cached = index.collect("readthedocs", source, knowledge, refresh=False)

        assert len(asked) == requests

    assert fetched.origin == "fetched"
    assert cached.origin == "cache"
    assert [d.url for d in cached.documents] == [d.url for d in fetched.documents]


def test_a_failed_fetch_falls_back_to_the_cache(knowledge):
    pages = {
        "/docs/contents.html": _contents("leases.html"),
        "/docs/leases.html": PAGE,
    }
    with serving_pages(pages) as (base, _):
        source = _sphinx(base)
        index.collect("readthedocs", source, knowledge, refresh=True)

    collected = index.collect("readthedocs", source, knowledge, refresh=True)

    assert collected.origin == "cache"
    assert collected.error is not None
    assert [d.title for d in collected.documents] == ["Leases"]


def test_an_unusable_cache_is_ignored(knowledge):
    cache = Path(knowledge.cache_dir) / "readthedocs.json"
    cache.parent.mkdir(parents=True)
    cache.write_text(json.dumps([{"nothing": "like a document"}]))

    collected = index.collect(
        "readthedocs", _sphinx("http://127.0.0.1:1"), knowledge, refresh=False
    )

    assert collected.documents == []


def test_embedding_fills_every_chunk_and_reports_the_width(knowledge):
    pages = {
        "/docs/contents.html": _contents("leases.html"),
        "/docs/leases.html": PAGE,
    }
    with serving_pages(pages) as (base, _):
        documents = chunks.split(
            fetch("readthedocs", _sphinx(base), knowledge), knowledge
        )

    width = index.embed(documents, FakeEmbedder(), knowledge)

    children = [c for d in documents for p in d.parents for c in p.chunks]
    assert width == 64
    assert all(len(c.embedding) == 64 for c in children)


def test_embedding_nothing_is_refused(knowledge):
    with pytest.raises(KnowledgeError, match="no chunks"):
        index.embed([], FakeEmbedder(), knowledge)
