from __future__ import annotations

import pytest

from scripts.import_public_court import CourtPage


def test_court_page_extracts_article_and_published_date():
    page = CourtPage()
    page.feed(
        '<div class="title">Public title</div>'
        '<li class="fl">发布时间：2026-08-06 16:15:58</li>'
        '<div class="txt_txt"><p>' + "article text " * 20 + '</p><p>second<br/>line</p></div>'
        '<div>unrelated footer</div>'
    )
    title, text, published_at = page.document()
    assert title == "Public title"
    assert published_at == "2026-08-06 16:15:58"
    assert "second\nline" in text
    assert "unrelated footer" not in text


@pytest.mark.parametrize("missing", ["title", "date", "text"])
def test_court_page_rejects_incomplete_source(missing):
    title = "" if missing == "title" else '<div class="title">Title</div>'
    date = "" if missing == "date" else "<li>发布时间：2026-08-06</li>"
    text = "short" if missing == "text" else "article " * 40
    page = CourtPage()
    page.feed(f'{title}{date}<div class="txt_txt">{text}</div>')
    with pytest.raises(ValueError):
        page.document()
