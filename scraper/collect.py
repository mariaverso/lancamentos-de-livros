#!/usr/bin/env python3
"""Coleta conservadora de livros anunciados em fontes públicas.

Princípios:
- Nunca inventa data: data ausente fica null.
- Falhas por fonte são registradas; nunca substituem um feed válido por [].
- Sem ISBN: identidade usa título + autor + URL da fonte, com deduplicação conservadora.
- O coletor genérico é uma base segura; seletores específicos devem ser calibrados por fonte.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
import unicodedata
from dataclasses import dataclass, asdict
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
FEED_PATH = ROOT / "feed.json"
REPORT_PATH = ROOT / "collection_report.json"
TIMEOUT = (8, 25)
USER_AGENT = os.getenv(
    "SCRAPER_USER_AGENT",
    "BookReleaseTracker/1.0 (polite public-page collector)"
)

# Configure aqui páginas públicas de calendários/listas. Não use páginas que exijam login.
# selectors podem ser adicionados por fonte depois de conferir o HTML real retornado.
SOURCES = [
    {"name": "Goodreads News", "url": "https://www.goodreads.com/blog", "enabled": True},
    {"name": "The Bookish Bulletin", "url": "https://thebookishbulletin.com/", "enabled": True},
    {"name": "Bookish Goblin", "url": "https://bookishgoblin.com/", "enabled": True},
    {"name": "New in Books", "url": "https://www.newinbooks.com/", "enabled": True},
    {"name": "BookBrowse", "url": "https://www.bookbrowse.com/", "enabled": True},
    # Arquivos oficiais dos clubes de seleção mensal
    {"name": "Book of the Month", "url": "https://www.bookofthemonth.com/all-months", "enabled": True},
    {"name": "Aardvark Book Club", "url": "https://www.aardvarkbookclub.com/allbooks", "enabled": True},
]
# O coletor tenta apenas links com texto que pareça título/entrada editorial.
# Para sites que usam JavaScript, será preciso RSS/API pública ou integração específica permitida.

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"})


@dataclass
class Book:
    title: str
    author: str | None = None
    publisher: str | None = None
    publication_date: str | None = None
    publication_status: str = "unknown"
    description: str | None = None
    genres: list[str] | None = None
    keywords: list[str] | None = None
    cover_url: str | None = None
    source_name: str | None = None
    source_url: str | None = None
    record_url: str | None = None
    record_type: str = "unknown"  # new_work | new_edition | unknown
    updated_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["genres"] = d["genres"] or []
        d["keywords"] = d["keywords"] or []
        return d


def clean_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, list):
        value = ", ".join(str(x) for x in value if x)
    value = re.sub(r"\s+", " ", str(value)).strip()
    return value or None


def parse_date(value: Any) -> str | None:
    """Return ISO date only for a real date found in source data; never substitutes today."""
    if not value:
        return None
    if isinstance(value, (dict, list)):
        return None
    s = str(value).strip()
    # ISO dates / timestamps
    m = re.search(r"\b(20\d{2})[-/](\d{1,2})[-/](\d{1,2})\b", s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
        except ValueError:
            return None
    # Common English written dates
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def publication_status(pub_date: str | None, today: date | None = None) -> str:
    if not pub_date:
        return "unknown"
    today = today or date.today()
    try:
        d = date.fromisoformat(pub_date)
    except ValueError:
        return "unknown"
    if d > today:
        return "upcoming"
    return "published"


def canonical_url(url: str | None, base: str) -> str | None:
    if not url:
        return None
    u = urljoin(base, url.strip())
    p = urlparse(u)
    if p.scheme not in ("http", "https") or not p.netloc:
        return None
    return u.split("#", 1)[0]


def image_url(node, base_url: str) -> str | None:
    if not node:
        return None
    candidates = []
    for attr in ("src", "data-src", "data-lazy-src", "data-original", "data-url"):
        if node.get(attr):
            candidates.append(node.get(attr))
    srcset = node.get("srcset") or node.get("data-srcset")
    if srcset:
        candidates.extend(part.strip().split()[0] for part in srcset.split(",") if part.strip())
    for candidate in candidates:
        url = canonical_url(candidate, base_url)
        if url and not url.startswith("data:"):
            return url
    return None


def is_probable_book_title(title: str) -> bool:
    t = title.strip()
    if len(t) < 3 or len(t) > 180:
        return False
    low = t.casefold()
    bad = ("read more", "click here", "subscribe", "sign in", "goodreads", "home", "next page")
    if low in bad:
        return False
    return bool(re.search(r"[A-Za-z]", t))


def iter_jsonld(soup: BeautifulSoup):
    for node in soup.select('script[type="application/ld+json"]'):
        raw = node.string or node.get_text()
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            item = stack.pop()
            if isinstance(item, list):
                stack.extend(item)
            elif isinstance(item, dict):
                if isinstance(item.get("@graph"), list):
                    stack.extend(item["@graph"])
                yield item


def extract_structured_books(soup: BeautifulSoup, source: dict, final_url: str) -> list[Book]:
    out = []
    for item in iter_jsonld(soup):
        typ = item.get("@type", [])
        types = {str(x).casefold() for x in (typ if isinstance(typ, list) else [typ])}
        # Do not turn every blog post into a book; require explicit Book schema.
        if "book" not in types:
            continue
        title = clean_text(item.get("name") or item.get("headline"))
        if not title or not is_probable_book_title(title):
            continue
        author = item.get("author")
        if isinstance(author, dict):
            author = author.get("name")
        elif isinstance(author, list):
            author = ", ".join(clean_text(x.get("name") if isinstance(x, dict) else x) or "" for x in author).strip(", ")
        pub_date = parse_date(item.get("datePublished") or item.get("publicationDate"))
        publisher = item.get("publisher")
        if isinstance(publisher, dict):
            publisher = publisher.get("name")
        image = item.get("image")
        if isinstance(image, dict):
            image = image.get("url")
        elif isinstance(image, list):
            image = image[0] if image else None
        desc = clean_text(item.get("description"))
        url = canonical_url(item.get("url"), final_url) or final_url
        out.append(Book(
            title=title, author=clean_text(author), publisher=clean_text(publisher),
            publication_date=pub_date, publication_status=publication_status(pub_date),
            description=desc, cover_url=canonical_url(image, final_url),
            source_name=source["name"], source_url=source["url"], record_url=url,
            updated_at=datetime.now().astimezone().isoformat(timespec="seconds")
        ))
    return out


def extract_article_candidates(soup: BeautifulSoup, source: dict, final_url: str) -> list[Book]:
    """Fallback: collect article/title links without fabricating bibliographic fields."""
    out = []
    seen = set()
    for a in soup.select("article h1 a[href], article h2 a[href], article h3 a[href], h1 a[href], h2 a[href], h3 a[href]"):
        title = clean_text(a.get_text(" ", strip=True))
        href = canonical_url(a.get("href"), final_url)
        if not title or not href or not is_probable_book_title(title) or href in seen:
            continue
        seen.add(href)
        article = a.find_parent("article")
        container = article or a.parent
        desc = None
        cover = None
        if container:
            p = container.select_one("p")
            desc = clean_text(p.get_text(" ", strip=True)) if p else None
            img = container.select_one("img")
            cover = image_url(img, final_url)
        # This is an article lead, not a confirmed book record. Keep status/date unknown.
        out.append(Book(
            title=title, description=desc, cover_url=cover,
            publication_date=None, publication_status="unknown",
            source_name=source["name"], source_url=source["url"], record_url=href,
            updated_at=datetime.now().astimezone().isoformat(timespec="seconds")
        ))
    return out


def extract_club_books(soup: BeautifulSoup, source: dict, final_url: str) -> list[Book]:
    """Fallback para os arquivos dos clubes; não confunde mês de seleção com publicação."""
    out = []
    seen = set()
    for heading in soup.select("h2, h3, h4"):
        title = clean_text(heading.get_text(" ", strip=True))
        if not title or not is_probable_book_title(title):
            continue
        if re.fullmatch(r"(?i)(january|february|march|april|may|june|july|august|september|october|november|december)\s+20\d{2}", title):
            continue
        container = heading.find_parent(["article", "li"]) or heading.parent
        if not container:
            continue
        author_node = container.select_one("[class*=author], [itemprop=author]")
        author = clean_text(author_node.get_text(" ", strip=True)) if author_node else None
        img = container.select_one("img")
        cover = image_url(img, final_url)
        desc_node = container.select_one("[class*=description], [class*=blurb], [class*=synopsis], p")
        desc = clean_text(desc_node.get_text(" ", strip=True)) if desc_node else None
        link = heading.find("a", href=True) or container.find("a", href=True)
        record_url = canonical_url(link.get("href"), final_url) if link else final_url
        # Capture only explicit genre/category chips; never infer them from synopsis prose.
        genres = []
        for tag_node in container.select("[class*=genre], [class*=tag], [class*=category], [data-genre]"):
            tag_value = clean_text(tag_node.get("data-genre") or tag_node.get_text(" ", strip=True))
            if tag_value and len(tag_value) <= 40 and tag_value.casefold() not in (title or "").casefold():
                if tag_value not in genres:
                    genres.append(tag_value)
        key = (normalize_key(title), normalize_key(author))
        if key in seen:
            continue
        seen.add(key)
        out.append(Book(
            title=title, author=author, publication_date=None,
            publication_status="unknown", description=desc, cover_url=cover,
            genres=genres, source_name=source["name"], source_url=source["url"], record_url=record_url,
            record_type="unknown",
            updated_at=datetime.now().astimezone().isoformat(timespec="seconds")
        ))
    return out


def fetch_source(source: dict) -> tuple[list[Book], dict]:
    report = {"source": source["name"], "url": source["url"], "status": "failed", "records": 0, "error": None}
    try:
        response = SESSION.get(source["url"], timeout=TIMEOUT)
        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "").lower()
        if "html" not in content_type and not response.text.lstrip().startswith("<"):
            raise ValueError(f"Resposta não parece HTML (Content-Type: {content_type or 'ausente'})")
        soup = BeautifulSoup(response.content, "html.parser")
        records = extract_structured_books(soup, source, response.url)
        if not records and source["name"] in ("Book of the Month", "Aardvark Book Club"):
            records = extract_club_books(soup, source, response.url)
        if not records:
            records = extract_article_candidates(soup, source, response.url)
        report.update(status="ok" if records else "no_records", records=len(records), final_url=response.url)
        return records, report
    except (requests.RequestException, ValueError) as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        return [], report


def normalize_key(text: str | None) -> str:
    text = unicodedata.normalize("NFKD", (text or "").casefold())
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def dedupe(records: list[Book]) -> list[Book]:
    """Conservative dedupe by normalized title + author. No destructive title truncation."""
    result: list[Book] = []
    positions: dict[tuple[str, str], int] = {}
    for book in records:
        key = (normalize_key(book.title), normalize_key(book.author))
        if not key[0]:
            continue
        provenance = {
            "name": book.source_name, "url": book.source_url, "record_url": book.record_url
        }
        if not hasattr(book, "_sources"):
            book._sources = [provenance] if any(provenance.values()) else []
        if key not in positions:
            positions[key] = len(result)
            result.append(book)
            continue
        current = result[positions[key]]
        # Merge only verified non-empty fields and preserve every source URL.
        for field in ("publisher", "publication_date", "description", "cover_url", "author"):
            if not getattr(current, field) and getattr(book, field):
                setattr(current, field, getattr(book, field))
        current.genres = list(dict.fromkeys((current.genres or []) + (book.genres or [])))
        current.keywords = list(dict.fromkeys((current.keywords or []) + (book.keywords or [])))
        for source_item in getattr(book, "_sources", [provenance]):
            if source_item not in current._sources:
                current._sources.append(source_item)
        if current.publication_date:
            current.publication_status = publication_status(current.publication_date)
    return result


def load_previous_feed() -> list[dict]:
    try:
        data = json.loads(FEED_PATH.read_text(encoding="utf-8"))
        return data.get("books", []) if isinstance(data, dict) else []
    except (OSError, json.JSONDecodeError, AttributeError):
        return []


def main() -> int:
    collected: list[Book] = []
    reports = []
    for source in SOURCES:
        if not source.get("enabled", True):
            continue
        records, report = fetch_source(source)
        collected.extend(records)
        reports.append(report)
        time.sleep(1.0)  # avoid rapid repeated requests
    # Article candidates can be useful leads, but aren't promoted to confirmed books
    # unless structured Book metadata was found. They remain explicitly unverified.
    books = dedupe(collected)
    successes = sum(1 for r in reports if r["status"] == "ok")
    previous = load_previous_feed()
    report_doc = {
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "sources": reports,
        "records_collected": len(collected),
        "records_after_deduplication": len(books),
        "successful_sources": successes,
        "warning": None,
    }
    REPORT_PATH.write_text(json.dumps(report_doc, ensure_ascii=False, indent=2), encoding="utf-8")

    # Guard against erasing a previously good feed after source failures or empty extraction.
    if not books or successes == 0:
        report_doc["warning"] = "Feed anterior preservado: nenhuma coleta utilizável nesta execução."
        REPORT_PATH.write_text(json.dumps(report_doc, ensure_ascii=False, indent=2), encoding="utf-8")
        logging.error(report_doc["warning"])
        return 2

    # Add explicit provenance and a stable UI-compatible short description.
    output = []
    for book in books:
        d = book.to_dict()
        d["sources"] = getattr(book, "_sources", [{"name": book.source_name, "url": book.source_url, "record_url": book.record_url}])
        d["keywords_display"] = " + ".join(
            x.upper() for x in (d.get("genres", []) + d.get("keywords", [])) if x
        )
        output.append(d)
    feed = {
        "schema_version": 2,
        "updated_at": report_doc["updated_at"],
        "books": output,
    }
    tmp = FEED_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(feed, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(FEED_PATH)
    logging.info("Feed atualizado: %s livros; %s fontes com registros.", len(output), successes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
