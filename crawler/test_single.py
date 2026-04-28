"""Quick local test: fetch and parse a single Neocities site, no DB required."""

import asyncio
import logging
import sys

import httpx

from crawler.crawl import (
    check_robots,
    discover_links,
    extract_images,
    extract_text,
    url_to_path,
)
from crawler.http import USER_AGENT

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("test_single")


async def test_site(sitename: str, max_pages: int = 5):
    site_url = f"https://{sitename}.neocities.org"
    logger.info(f"Testing {site_url}")

    async with httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT},
        timeout=httpx.Timeout(30),
        follow_redirects=True,
    ) as client:
        # Check robots.txt
        allowed = await check_robots(client, site_url)
        logger.info(f"robots.txt: {'allowed' if allowed else 'BLOCKED'}")
        if not allowed:
            return

        visited = set()
        to_visit = {site_url, site_url + "/"}
        pages_processed = 0

        while to_visit and pages_processed < max_pages:
            url = to_visit.pop()
            if url in visited:
                continue
            visited.add(url)

            try:
                response = await client.get(url)
                logger.info(f"GET {url} → {response.status_code}")
            except Exception as exc:
                logger.error(f"Fetch failed {url}: {type(exc).__name__}: {exc}")
                continue

            if response.status_code != 200:
                continue

            content_type = response.headers.get("content-type", "")
            if "text/html" not in content_type:
                logger.debug(f"Skipping non-HTML: {url} ({content_type})")
                continue

            html = response.text

            # Test text extraction
            try:
                extracted = extract_text(html, url)
                if extracted:
                    logger.info(
                        f"  Extracted: title={extracted['title']!r}, "
                        f"words={extracted['word_count']}, "
                        f"desc={extracted['description']!r:.80}"
                    )
                else:
                    logger.info(f"  No text extracted from {url}")
            except Exception as exc:
                logger.error(f"  extract_text FAILED: {type(exc).__name__}: {exc}")

            # Test image extraction
            try:
                images = extract_images(html, url)
                logger.info(f"  Images found: {len(images)}")
            except Exception as exc:
                logger.error(f"  extract_images FAILED: {type(exc).__name__}: {exc}")

            # Test link discovery
            try:
                links = discover_links(html, url)
                new_links = links - visited
                to_visit.update(new_links)
                logger.info(f"  Links: {len(links)} total, {len(new_links)} new")
            except Exception as exc:
                logger.error(f"  discover_links FAILED: {type(exc).__name__}: {exc}")

            # Test path generation
            try:
                path = url_to_path(url)
                logger.info(f"  Path: {path}")
            except Exception as exc:
                logger.error(f"  url_to_path FAILED: {type(exc).__name__}: {exc}")

            pages_processed += 1

    logger.info(f"Done. Processed {pages_processed} pages from {sitename}")


if __name__ == "__main__":
    sitename = sys.argv[1] if len(sys.argv) > 1 else "amandabsolutions"
    max_pages = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    asyncio.run(test_site(sitename, max_pages))
