import argparse
import asyncio
import logging
import urllib.parse
from urllib.robotparser import RobotFileParser

import httpx
import trafilatura
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from crawler import db, storage
from crawler.http import (
    CrawlerError,
    USER_AGENT,
    fetch_with_retry,
    make_client,
    setup_signal_handlers,
    shutdown_event,
)

logger = logging.getLogger(__name__)

# File extensions to skip when discovering sub-pages
SKIP_EXTENSIONS = frozenset({
    ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".ico", ".bmp",
    ".css", ".js", ".json", ".xml", ".rss", ".atom",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".zip", ".tar", ".gz", ".rar", ".7z",
    ".mp3", ".mp4", ".wav", ".ogg", ".webm", ".avi", ".mov",
    ".woff", ".woff2", ".ttf", ".eot", ".otf",
    ".txt", ".csv", ".tsv",
})


# ---------------------------------------------------------------------------
# robots.txt
# ---------------------------------------------------------------------------

async def check_robots(client: httpx.AsyncClient, site_url: str) -> bool:
    """Check if our crawler is allowed to access a site. Returns True if allowed."""
    robots_url = f"{site_url.rstrip('/')}/robots.txt"
    rp = RobotFileParser()

    try:
        response = await client.get(robots_url)
        if response.status_code == 200:
            rp.parse(response.text.splitlines())
            return rp.can_fetch(USER_AGENT, site_url)
        # No robots.txt or error → assume allowed
        return True
    except (httpx.HTTPError, Exception):
        # Can't fetch robots.txt → assume allowed
        return True


# ---------------------------------------------------------------------------
# Link discovery
# ---------------------------------------------------------------------------

def discover_links(html: str, base_url: str) -> set[str]:
    """Extract same-domain internal links from HTML."""
    soup = BeautifulSoup(html, "html.parser")
    base_parsed = urllib.parse.urlparse(base_url)
    base_domain = base_parsed.netloc
    links = set()

    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"].strip()
        if not href or href.startswith("#") or href.startswith("mailto:") or href.startswith("javascript:"):
            continue

        # Resolve relative URLs
        full_url = urllib.parse.urljoin(base_url, href)
        parsed = urllib.parse.urlparse(full_url)

        # Same domain only
        if parsed.netloc != base_domain:
            continue

        # Strip fragment and query
        clean_url = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))

        # Skip non-HTML extensions
        path_lower = parsed.path.lower()
        ext = "." + path_lower.rsplit(".", 1)[-1] if "." in path_lower.rsplit("/", 1)[-1] else ""
        if ext in SKIP_EXTENSIONS:
            continue

        links.add(clean_url)

    return links


def url_to_path(url: str) -> str:
    """Extract the path portion of a URL for use as the document path."""
    parsed = urllib.parse.urlparse(url)
    path = parsed.path or "/"
    if path == "/" or path.endswith("/"):
        path = path + "index.html"
    return path


# ---------------------------------------------------------------------------
# Text extraction
# ---------------------------------------------------------------------------

def extract_text(html: str, url: str) -> dict | None:
    """Extract clean text and metadata from HTML using trafilatura.

    Returns dict with title, body, description, author, word_count, or None if extraction fails.
    """
    body = trafilatura.extract(
        html,
        url=url,
        favor_recall=True,
        include_comments=False,
        include_tables=True,
        deduplicate=True,
    )

    if not body or len(body.strip()) < 10:
        return None

    metadata = trafilatura.extract(
        html,
        url=url,
        favor_recall=True,
        output_format="json",
        include_comments=False,
    )

    title = None
    description = None
    author = None
    if metadata:
        import json
        try:
            meta = json.loads(metadata)
            title = meta.get("title")
            description = meta.get("description")
            author = meta.get("author")
        except (json.JSONDecodeError, TypeError):
            pass

    # Fallback: extract title from HTML if trafilatura didn't find one
    if not title:
        soup = BeautifulSoup(html, "html.parser")
        title_tag = soup.find("title")
        if title_tag:
            title = title_tag.get_text(strip=True)

    word_count = len(body.split())

    return {
        "title": title,
        "body": body,
        "description": description,
        "author": author,
        "word_count": word_count,
    }


# ---------------------------------------------------------------------------
# Image extraction
# ---------------------------------------------------------------------------

MIN_IMAGE_DIMENSION = 64  # pixels — filter out icons/spacers


def _parse_int_attr(val) -> int | None:
    if val is None:
        return None
    try:
        return int(str(val).replace("px", "").strip())
    except (ValueError, TypeError):
        return None


def extract_images(html: str, page_url: str) -> list[dict]:
    """Extract image metadata from HTML, filtering out tiny/icon images."""
    soup = BeautifulSoup(html, "html.parser")
    images = []
    seen_srcs = set()

    for img in soup.find_all("img", src=True):
        src = img["src"].strip()
        if not src or src.startswith("data:"):
            continue

        abs_src = urllib.parse.urljoin(page_url, src)

        if abs_src in seen_srcs:
            continue
        seen_srcs.add(abs_src)

        # Skip SVGs and ICOs
        path_lower = urllib.parse.urlparse(abs_src).path.lower()
        if path_lower.endswith((".svg", ".ico")):
            continue

        width = _parse_int_attr(img.get("width"))
        height = _parse_int_attr(img.get("height"))

        if width is not None and width < MIN_IMAGE_DIMENSION:
            continue
        if height is not None and height < MIN_IMAGE_DIMENSION:
            continue

        alt = (img.get("alt") or "").strip() or None
        title = (img.get("title") or "").strip() or None

        caption = None
        figure = img.find_parent("figure")
        if figure:
            figcaption = figure.find("figcaption")
            if figcaption:
                caption = figcaption.get_text(strip=True) or None

        images.append({
            "src": abs_src,
            "alt": alt,
            "title": title,
            "caption": caption,
            "width": width,
            "height": height,
            "page_url": page_url,
        })

    return images


# ---------------------------------------------------------------------------
# Per-site crawl
# ---------------------------------------------------------------------------

async def crawl_site(
    client: httpx.AsyncClient,
    pool,
    site_id: int,
    site_url: str,
    sitename: str,
    max_pages: int,
    crawl_delay: float,
) -> int:
    """Crawl a single site: fetch pages, extract text, store results.

    Returns the number of pages successfully processed.
    """
    # Check robots.txt
    allowed = await check_robots(client, site_url)
    if not allowed:
        logger.info(f"[{sitename}] Blocked by robots.txt, skipping")
        await db.mark_site_blocked(pool, site_id)
        return 0

    visited = set()
    to_visit = {site_url.rstrip("/") + "/"}
    pages_processed = 0

    while to_visit and pages_processed < max_pages:
        if shutdown_event.is_set():
            break

        url = to_visit.pop()
        if url in visited:
            continue
        visited.add(url)

        # Fetch the page
        try:
            response = await fetch_with_retry(client, url)
        except (CrawlerError, httpx.HTTPStatusError) as exc:
            logger.debug(f"[{sitename}] Failed to fetch {url}: {exc}")
            continue

        # Only process HTML responses
        content_type = response.headers.get("content-type", "")
        if "text/html" not in content_type:
            logger.debug(f"[{sitename}] Skipping non-HTML: {url} ({content_type})")
            continue

        html = response.text

        # Discover sub-page links
        new_links = discover_links(html, url)
        for link in new_links:
            if link not in visited:
                to_visit.add(link)

        # Extract text
        extracted = extract_text(html, url)
        if not extracted:
            logger.debug(f"[{sitename}] No text extracted from {url}")
            continue

        # Store compressed full text
        path = url_to_path(url)
        storage_key = storage.make_key(site_id, path)
        storage.store_text(storage_key, extracted["body"])

        # Save document metadata to DB
        await db.upsert_document(pool, {
            "site_id": site_id,
            "url": url,
            "path": path,
            "title": extracted["title"],
            "body_snippet": extracted["body"][:500] if extracted["body"] else None,
            "description": extracted["description"],
            "author": extracted["author"],
            "word_count": extracted["word_count"],
            "storage_key": storage_key,
        })

        # Extract and store image metadata
        images = extract_images(html, url)
        if images:
            doc_id = await db.get_document_id(pool, site_id, path)
            await db.upsert_images(pool, site_id, doc_id, images)

        pages_processed += 1
        logger.debug(f"[{sitename}] {pages_processed}/{max_pages} — {url} ({extracted['word_count']} words, {len(images)} images)")

        await asyncio.sleep(crawl_delay)

    return pages_processed


# ---------------------------------------------------------------------------
# Main crawl loop
# ---------------------------------------------------------------------------

async def run_crawl(pool, args: argparse.Namespace) -> None:
    """Pull sites from the queue and crawl them."""
    await db.recover_stale_queue(pool)

    async with make_client() as client:
        sites_crawled = 0
        total_pages = 0
        remaining = args.max_sites

        while True:
            if shutdown_event.is_set():
                logger.info("Shutdown requested, stopping crawl")
                break

            batch_limit = min(10, remaining) if remaining else 10
            sites = await db.get_next_queued_sites(pool, limit=batch_limit)

            if not sites:
                logger.info("No more sites in queue")
                break

            for site in sites:
                if shutdown_event.is_set():
                    break

                site_id = site["site_id"]
                site_url = site["url"]
                sitename = site["sitename"]

                logger.info(f"Crawling [{sitename}] {site_url}")
                await db.update_queue_status(pool, site_id, "in_progress")

                try:
                    page_count = await crawl_site(
                        client, pool, site_id, site_url, sitename,
                        max_pages=args.max_pages_per_site,
                        crawl_delay=args.crawl_delay,
                    )
                    await db.mark_site_crawled(pool, site_id, page_count)
                    sites_crawled += 1
                    total_pages += page_count
                    logger.info(f"[{sitename}] Done — {page_count} pages extracted")

                except Exception as exc:
                    logger.error(f"[{sitename}] Crawl failed: {type(exc).__name__}: {exc}")
                    await db.update_queue_status(pool, site_id, "failed")

            if remaining is not None:
                remaining -= len(sites)
                if remaining <= 0:
                    break

        stats = await db.get_crawl_stats(pool)
        logger.info(
            f"Crawl session complete. "
            f"Sites crawled: {sites_crawled} | Pages extracted: {total_pages} | "
            f"Queue pending: {stats['pending']} | Documents total: {stats['documents']}"
        )

        await db.record_crawl_stats(pool, sites_crawled, total_pages)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="IndySearch Phase 3: Site Crawler")
    parser.add_argument("--max-sites", type=int, default=None, help="Limit sites to crawl (testing)")
    parser.add_argument("--max-pages-per-site", type=int, default=50, help="Max pages per site (default: 50)")
    parser.add_argument("--crawl-delay", type=float, default=1.5, help="Seconds between requests (default: 1.5)")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser.parse_args()


async def main(args: argparse.Namespace) -> None:
    load_dotenv()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    setup_signal_handlers()

    pool = await db.create_pool()
    await db.init_schema(pool)

    try:
        await run_crawl(pool, args)
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main(parse_args()))
