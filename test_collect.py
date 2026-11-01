import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scraper"))
import collect


def test_missing_date_stays_missing():
    assert collect.parse_date(None) is None
    assert collect.parse_date("date not listed") is None
    assert collect.publication_status(None, today=date(2026, 11, 1)) == "unknown"


def test_valid_dates():
    assert collect.parse_date("2027-02-03") == "2027-02-03"
    assert collect.parse_date("November 3, 2026") == "2026-11-03"
    assert collect.parse_date("2026-13-40") is None


def test_dedupe_does_not_strip_subtitles_or_parentheses():
    a = collect.Book(title="The House: A Novel", author="A. Writer")
    b = collect.Book(title="The House: A Memoir", author="A. Writer")
    c = collect.Book(title="The House (Illustrated Edition)", author="A. Writer")
    assert len(collect.dedupe([a, b, c])) == 3


def test_dedupe_same_title_author():
    a = collect.Book(title="The Book", author="A. Writer", publisher="Publisher A")
    b = collect.Book(title="The Book", author="A. Writer", cover_url="https://example.org/cover.jpg")
    out = collect.dedupe([a, b])
    assert len(out) == 1
    assert out[0].publisher == "Publisher A"
    assert out[0].cover_url == "https://example.org/cover.jpg"


def test_lazy_image_and_relative_url():
    from bs4 import BeautifulSoup
    soup = BeautifulSoup('<img data-src="/covers/a.jpg" srcset="/small.jpg 1x, /large.jpg 2x">', "html.parser")
    assert collect.image_url(soup.img, "https://example.org/books/") == "https://example.org/covers/a.jpg"


def test_unknown_date_not_marked_upcoming():
    assert collect.publication_status(None, today=date(2026, 11, 1)) == "unknown"
    assert collect.publication_status("2027-01-01", today=date(2026, 11, 1)) == "upcoming"
    assert collect.publication_status("2026-01-01", today=date(2026, 11, 1)) == "published"


def test_dedupe_preserves_all_source_provenance():
    a = collect.Book(title="The Book", author="A. Writer", source_name="Club A",
                     source_url="https://a.example/list", record_url="https://a.example/book")
    b = collect.Book(title="The Book", author="A. Writer", source_name="Club B",
                     source_url="https://b.example/list", record_url="https://b.example/book",
                     genres=["Fantasy"])
    out = collect.dedupe([a, b])
    assert len(out) == 1
    assert len(out[0]._sources) == 2
    assert out[0].genres == ["Fantasy"]


def test_club_parser_keeps_month_as_not_publication_date_and_extracts_genre():
    from bs4 import BeautifulSoup
    html = '''<article><h3><a href="/books/example">Example Book</a></h3>
    <span class="author">A. Writer</span><p class="description">A short synopsis.</p>
    <img data-src="/covers/example.jpg"><span class="genre">Fantasy</span></article>'''
    source = {"name": "Aardvark Book Club", "url": "https://club.example/allbooks"}
    records = collect.extract_club_books(BeautifulSoup(html, "html.parser"), source, source["url"])
    assert len(records) == 1
    assert records[0].title == "Example Book"
    assert records[0].author == "A. Writer"
    assert records[0].publication_date is None
    assert records[0].publication_status == "unknown"
    assert records[0].genres == ["Fantasy"]
    assert records[0].cover_url == "https://club.example/covers/example.jpg"
