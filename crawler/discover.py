import argparse
import asyncio
import logging
import urllib.parse
from email.utils import parsedate_to_datetime

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from crawler import db
from crawler.http import (
    CrawlerError,
    fetch_with_retry,
    make_client,
    setup_signal_handlers,
    shutdown_event,
)

logger = logging.getLogger(__name__)

BROWSE_URL = "https://neocities.org/browse"
API_INFO_URL = "https://neocities.org/api/info"


# ---------------------------------------------------------------------------
# Phase 1a — Browse page scraping
# ---------------------------------------------------------------------------

async def detect_last_page(client: httpx.AsyncClient) -> int:
    """Fetch page 1 and parse pagination to find the max page number."""
    response = await fetch_with_retry(client, BROWSE_URL, params={"sort_by": "newest", "page": 1})
    soup = BeautifulSoup(response.text, "html.parser")

    max_page = 1
    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"]
        if "page=" in href:
            parsed = urllib.parse.urlparse(href)
            params = urllib.parse.parse_qs(parsed.query)
            if "page" in params:
                try:
                    page_num = int(params["page"][0])
                    max_page = max(max_page, page_num)
                except ValueError:
                    continue
    return max_page


def parse_site_listings(soup: BeautifulSoup) -> list[dict]:
    """Extract site data from all <li> listings on a browse page."""
    sites = []

    for li in soup.find_all("li"):
        profile_link = li.find("a", href=lambda h: h and h.startswith("/site/"))
        if not profile_link:
            continue

        sitename = profile_link["href"].split("/site/")[-1]
        if not sitename:
            continue

        # Site URL from the first full-URL link
        site_url = None
        custom_domain = None
        for a in li.find_all("a", href=True):
            href = a["href"]
            if href.startswith("https://") or href.startswith("http://"):
                site_url = href
                if ".neocities.org" not in href:
                    custom_domain = urllib.parse.urlparse(href).netloc
                break

        if not site_url:
            site_url = f"https://{sitename}.neocities.org"

        # Visitor count
        visitors_link = li.find("a", title="Visitors")
        hits = 0
        if visitors_link:
            hits_text = visitors_link.get_text(strip=True).replace(",", "")
            try:
                hits = int(hits_text)
            except ValueError:
                hits = 0

        # Tags
        tags = []
        for tag_link in li.find_all("a", href=lambda h: h and "browse?tag=" in h):
            tag_text = tag_link.get_text(strip=True)
            if tag_text:
                tags.append(tag_text)

        sites.append({
            "sitename": sitename,
            "url": site_url,
            "custom_domain": custom_domain,
            "tags": tags,
            "hits": hits,
        })

    return sites


async def scrape_browse_page(client: httpx.AsyncClient, page_num: int) -> list[dict]:
    """Fetch and parse one browse page."""
    response = await fetch_with_retry(
        client, BROWSE_URL,
        params={"sort_by": "newest", "page": page_num},
    )
    soup = BeautifulSoup(response.text, "html.parser")
    return parse_site_listings(soup)


async def run_phase_1a(pool, args: argparse.Namespace) -> None:
    """Phase 1a: Scrape browse pages to build the complete domain list."""
    async with make_client() as client:
        last_page = await detect_last_page(client)
        end_page = min(last_page, args.start_page + args.max_pages - 1) if args.max_pages else last_page

        logger.info(f"Phase 1a: scraping pages {args.start_page}–{end_page} (of {last_page} total)")

        total_inserted = 0
        for page_num in range(args.start_page, end_page + 1):
            if shutdown_event.is_set():
                logger.info("Shutdown requested, stopping browse scrape")
                break

            try:
                sites = await scrape_browse_page(client, page_num)
                inserted = await db.upsert_sites(pool, sites)
                total_inserted += inserted
            except CrawlerError as exc:
                logger.error(f"Skipping page {page_num}: {exc}")
                continue

            if page_num % 100 == 0 or page_num == end_page:
                stats = await db.get_discovery_stats(pool)
                logger.info(
                    f"Page {page_num}/{end_page} | "
                    f"This batch: {inserted} new | Total in DB: {stats['total']}"
                )

            await asyncio.sleep(args.browse_delay)

        stats = await db.get_discovery_stats(pool)
        logger.info(f"Phase 1a complete. {total_inserted} new sites added. DB total: {stats['total']}")


# ---------------------------------------------------------------------------
# Phase 1b — API enrichment
# ---------------------------------------------------------------------------

def parse_api_response(info: dict) -> dict:
    """Transform API /info response into our DB metadata format."""
    created_at = None
    if info.get("created_at"):
        try:
            created_at = parsedate_to_datetime(info["created_at"])
        except Exception:
            pass

    last_updated = None
    if info.get("last_updated"):
        try:
            last_updated = parsedate_to_datetime(info["last_updated"])
        except Exception:
            pass

    return {
        "hits": info.get("hits", 0),
        "tags": info.get("tags", []),
        "created_at": created_at,
        "last_updated": last_updated,
        "custom_domain": info.get("domain"),
    }


async def fetch_site_info(client: httpx.AsyncClient, sitename: str) -> dict | None:
    """Call /api/info for one site. Returns parsed metadata or None."""
    try:
        response = await fetch_with_retry(client, API_INFO_URL, params={"sitename": sitename})
        data = response.json()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 400:
            logger.debug(f"Site not found via API: {sitename}")
            return None
        raise

    if data.get("result") != "success":
        logger.warning(f"API non-success for {sitename}: {data}")
        return None

    return parse_api_response(data["info"])


async def run_phase_1b(pool, args: argparse.Namespace) -> None:
    """Phase 1b: Enrich sites with Neocities API metadata."""
    async with make_client() as client:
        enriched_count = 0
        failed_count = 0
        remaining = args.enrich_limit

        while True:
            if shutdown_event.is_set():
                logger.info("Shutdown requested, stopping enrichment")
                break

            batch_limit = min(500, remaining) if remaining else 500
            sites = await db.get_unenriched_sites(pool, limit=batch_limit)

            if not sites:
                break

            for site in sites:
                if shutdown_event.is_set():
                    break

                sitename = site["sitename"]
                try:
                    metadata = await fetch_site_info(client, sitename)
                    if metadata:
                        await db.update_site_metadata(pool, sitename, metadata)
                        enriched_count += 1
                    else:
                        failed_count += 1
                except Exception:
                    logger.exception(f"Failed to enrich {sitename}")
                    failed_count += 1

                if enriched_count % 100 == 0 and enriched_count > 0:
                    stats = await db.get_discovery_stats(pool)
                    logger.info(
                        f"Enriched: {enriched_count} | Failed: {failed_count} | "
                        f"Remaining: {stats['unenriched']}"
                    )

                await asyncio.sleep(args.api_delay)

            if remaining is not None:
                remaining -= len(sites)
                if remaining <= 0:
                    break

        logger.info(f"Phase 1b complete. Enriched: {enriched_count}, Failed: {failed_count}")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="IndySearch Phase 1: Site Discovery")
    parser.add_argument(
        "--phase", choices=["1a", "1b", "all"], default="all",
        help="1a = browse scraping, 1b = API enrichment, all = both (default: all)",
    )
    parser.add_argument("--max-pages", type=int, default=None, help="Limit browse scraping to N pages (testing)")
    parser.add_argument("--start-page", type=int, default=1, help="Start browse scraping from this page (default: 1)")
    parser.add_argument("--enrich-limit", type=int, default=None, help="Limit API enrichment to N sites (testing)")
    parser.add_argument("--browse-delay", type=float, default=1.0, help="Seconds between browse page requests (default: 1.0)")
    parser.add_argument("--api-delay", type=float, default=0.3, help="Seconds between API requests (default: 0.3)")
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
        if args.phase in ("1a", "all"):
            await run_phase_1a(pool, args)
        if args.phase in ("1b", "all"):
            await run_phase_1b(pool, args)
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main(parse_args()))
