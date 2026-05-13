"""HTML scraper for Anthropic-side pages that don't expose native RSS.

Two discovery strategies:

* **Sitemap-based** (anthropic_news/engineering/research): fetch anthropic.com/sitemap.xml,
  filter URLs by path prefix. Sitemap <lastmod> is used ONLY for ordering — never as a
  published_at proxy, because Anthropic bumps lastmod on site-wide republishes.
* **Listing-based** (claude_blog): fetch claude.com/blog HTML, extract /blog/* hrefs.

For each discovered URL not already in raw_items, fetch the article and resolve
published_at via this fallback chain: og meta `article:published_time` → JSON-LD
`datePublished` (claude.com/blog) → body dateline scrape (anthropic.com — looks for
"Mon DD, YYYY" in a `body-3 agate` div) → NULL. We never fall back to sitemap
lastmod because that field is unreliable.
"""
import hashlib
import json
import logging
import re
from datetime import datetime, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.database import async_session
from app.models import RawItem, Source

logger = logging.getLogger(__name__)

UA = "Frontier-Monitor/1.0"
# Cap per-run fetches — applied AFTER dedup so each run pulls up to N truly-new articles
# (not just looks at the top-N newest URLs and re-checks them every time). This lets the
# scheduled 2h runs progressively backfill historical content over multiple cycles.
ARTICLE_LIMIT_PER_RUN = 30

_META_RE_TEMPLATE = (
    r'<meta[^>]*property=["\']{prop}["\'][^>]*content=["\']([^"\']+)["\']'
)
_META_RE_TEMPLATE_REVERSED = (
    r'<meta[^>]*content=["\']([^"\']+)["\'][^>]*property=["\']{prop}["\']'
)


def _entry_id(source_id: str, url: str) -> str:
    return hashlib.sha256(f"{source_id}|{url}".encode("utf-8")).hexdigest()


def _meta_content(html: str, prop: str) -> str | None:
    escaped = re.escape(prop)
    for tmpl in (_META_RE_TEMPLATE, _META_RE_TEMPLATE_REVERSED):
        m = re.search(tmpl.format(prop=escaped), html, re.IGNORECASE)
        if m:
            return m.group(1)
    return None


_DATE_TEXT_RE = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|October|November|December|"
    r"Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{1,2}),?\s+(20\d{2})\b"
)
_AGATE_DIV_RE = re.compile(
    r'<div[^>]*class=["\'][^"\']*\bagate\b[^"\']*["\'][^>]*>([^<]+)</div>',
    re.IGNORECASE,
)
_JSONLD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)


def _parse_date_text(s: str) -> datetime | None:
    m = _DATE_TEXT_RE.search(s)
    if not m:
        return None
    month, day, year = m.group(1), int(m.group(2)), int(m.group(3))
    for fmt in ("%B", "%b"):
        try:
            month_dt = datetime.strptime(month, fmt)
            return datetime(year, month_dt.month, day, tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _extract_jsonld_date(html: str) -> datetime | None:
    """Pull datePublished out of any application/ld+json block (claude.com/blog)."""
    for m in _JSONLD_RE.finditer(html):
        try:
            data = json.loads(m.group(1).strip())
        except (json.JSONDecodeError, ValueError):
            continue
        candidates = data if isinstance(data, list) else [data]
        for c in candidates:
            if not isinstance(c, dict):
                continue
            dp = c.get("datePublished") or c.get("dateCreated")
            if not dp:
                continue
            try:
                return datetime.fromisoformat(dp.replace("Z", "+00:00"))
            except ValueError:
                pass
            parsed = _parse_date_text(dp)
            if parsed:
                return parsed
    return None


def _extract_body_dateline(html: str) -> datetime | None:
    """Anthropic article pages have no date meta — only a `<div class='... agate'>Mon DD, YYYY</div>`."""
    for m in _AGATE_DIV_RE.finditer(html):
        parsed = _parse_date_text(m.group(1))
        if parsed:
            return parsed
    return None


def resolve_published_at(html: str, source_id: str) -> datetime | None:
    """og meta → JSON-LD → body dateline → None. Never lastmod."""
    pub_str = _meta_content(html, "article:published_time")
    if pub_str:
        try:
            return datetime.fromisoformat(pub_str.replace("Z", "+00:00"))
        except ValueError:
            pass
    jsonld = _extract_jsonld_date(html)
    if jsonld:
        return jsonld
    return _extract_body_dateline(html)


async def _fetch(client: httpx.AsyncClient, url: str) -> str | None:
    try:
        r = await client.get(url, timeout=30.0, follow_redirects=True)
        r.raise_for_status()
        return r.text
    except Exception as e:
        logger.warning("fetch %s failed: %s", url, e)
        return None


def _filter_sitemap(body: str, path_prefix: str) -> list[tuple[str, datetime | None]]:
    """Return (url, lastmod) for sitemap entries under the given path prefix, newest first."""
    out: list[tuple[str, datetime | None]] = []
    pat = re.compile(
        rf"<url>\s*<loc>(https://www\.anthropic\.com{re.escape(path_prefix)}[^<]+)</loc>"
        r"\s*(?:<lastmod>([^<]+)</lastmod>)?",
    )
    for m in pat.finditer(body):
        url = m.group(1)
        lm: datetime | None = None
        if m.group(2):
            try:
                lm = datetime.fromisoformat(m.group(2).replace("Z", "+00:00"))
            except ValueError:
                pass
        out.append((url, lm))
    out.sort(key=lambda x: x[1] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return out


async def _discover_via_sitemap(client: httpx.AsyncClient, path_prefix: str) -> list[tuple[str, datetime | None]]:
    body = await _fetch(client, "https://www.anthropic.com/sitemap.xml")
    if not body:
        return []
    return _filter_sitemap(body, path_prefix)


async def _discover_anthropic_news(client: httpx.AsyncClient) -> list[tuple[str, datetime | None]]:
    return await _discover_via_sitemap(client, "/news/")


async def _discover_anthropic_engineering(client: httpx.AsyncClient) -> list[tuple[str, datetime | None]]:
    return await _discover_via_sitemap(client, "/engineering/")


async def _discover_anthropic_research(client: httpx.AsyncClient) -> list[tuple[str, datetime | None]]:
    return await _discover_via_sitemap(client, "/research/")


async def _discover_claude_blog(client: httpx.AsyncClient) -> list[tuple[str, datetime | None]]:
    body = await _fetch(client, "https://claude.com/blog")
    if not body:
        return []
    seen: set[str] = set()
    out: list[tuple[str, datetime | None]] = []
    for m in re.finditer(r'href=["\'](/blog/[^"\'/?#]+)["\']', body):
        path = m.group(1)
        if path == "/blog" or path.startswith("/blog/category/"):
            continue
        url = f"https://claude.com{path}"
        if url in seen:
            continue
        seen.add(url)
        out.append((url, None))
    return out


DISCOVERERS = {
    "anthropic_news": _discover_anthropic_news,
    "anthropic_engineering": _discover_anthropic_engineering,
    "anthropic_research": _discover_anthropic_research,
    "claude_blog": _discover_claude_blog,
}


async def ingest_source(source_id: str) -> dict:
    discoverer = DISCOVERERS.get(source_id)
    if not discoverer:
        return {"source_id": source_id, "status": "no_discoverer"}

    async with async_session() as db:
        src = await db.get(Source, source_id)
        if not src:
            return {"source_id": source_id, "status": "missing"}
        if not src.enabled:
            return {"source_id": source_id, "status": "disabled"}

        async with httpx.AsyncClient(headers={"User-Agent": UA}) as client:
            urls = await discoverer(client)
            now = datetime.now(timezone.utc)

            # Dedup against existing raw_items first, THEN cap to ARTICLE_LIMIT_PER_RUN.
            # Sitemap order is newest-first, so capped slice = oldest unseen articles in the
            # bottom of the queue still get pulled on subsequent runs.
            ids_for_urls = {url: _entry_id(source_id, url) for url, _ in urls}
            existing_ids: set[str] = set()
            if ids_for_urls:
                rows = (await db.execute(
                    select(RawItem.id).where(RawItem.id.in_(list(ids_for_urls.values())))
                )).all()
                existing_ids = {r[0] for r in rows}

            new_urls = [(u, lm) for u, lm in urls if ids_for_urls[u] not in existing_ids]
            new_urls = new_urls[:ARTICLE_LIMIT_PER_RUN]
            inserted = 0
            for url, lastmod in new_urls:
                html = await _fetch(client, url)
                if not html:
                    continue
                title = (_meta_content(html, "og:title") or url.rsplit("/", 1)[-1]).strip()[:1000]
                desc = (_meta_content(html, "og:description") or "")[:50000]
                published = resolve_published_at(html, source_id)

                stmt = pg_insert(RawItem).values(
                    id=ids_for_urls[url],
                    source_id=source_id,
                    pillar=src.pillar,
                    url=url,
                    title=title,
                    author=None,
                    published_at=published,
                    fetched_at=now,
                    raw_text=desc,
                    raw_json={"og_description": desc, "discovery": source_id},
                ).on_conflict_do_nothing(index_elements=["id"])
                result = await db.execute(stmt)
                if result.rowcount:
                    inserted += 1

            src.last_fetched_at = now
            await db.commit()
            logger.info("anthropic_html %s: total_urls=%d new=%d inserted=%d",
                        source_id, len(urls), len(new_urls), inserted)
            return {
                "source_id": source_id,
                "status": "ok",
                "total_urls": len(urls),
                "new_articles": len(new_urls),
                "inserted": inserted,
            }


async def backfill_published_at(source_ids: list[str] | None = None) -> dict:
    """Re-fetch each anthropic/claude_blog raw_item and resolve published_at fresh.

    Cleans up rows that were stamped with sitemap lastmod (now-known-bad) or NULL.
    Updates only when the resolver returns a new value — leaves NULL when nothing
    can be extracted, rather than re-asserting a wrong date.
    """
    targets = source_ids or list(DISCOVERERS.keys())
    async with async_session() as db:
        rows = (
            await db.execute(
                select(RawItem.id, RawItem.url, RawItem.source_id, RawItem.published_at)
                .where(RawItem.source_id.in_(targets))
            )
        ).all()

        updated = 0
        unchanged = 0
        unresolved = 0
        async with httpx.AsyncClient(headers={"User-Agent": UA}) as client:
            for raw_id, url, source_id, current in rows:
                if not url:
                    unresolved += 1
                    continue
                html = await _fetch(client, url)
                if not html:
                    unresolved += 1
                    continue
                resolved = resolve_published_at(html, source_id)
                if resolved is None:
                    unresolved += 1
                    continue
                if current and abs((current - resolved).total_seconds()) < 60:
                    unchanged += 1
                    continue
                item = await db.get(RawItem, raw_id)
                if item:
                    item.published_at = resolved
                    updated += 1
            await db.commit()
        return {
            "scanned": len(rows),
            "updated": updated,
            "unchanged": unchanged,
            "unresolved": unresolved,
        }
