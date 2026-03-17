"""IndySearch Pipeline Orchestrator

Runs the full crawl-to-index pipeline:
  1. Discover sites (browse pages + API enrichment)
  2. Build crawl queue (with skip logic)
  3. Crawl sites + extract text
  4. Build Tantivy search index
"""
import argparse
import asyncio
import logging
import os
from argparse import Namespace

from dotenv import load_dotenv

from crawler import db
from crawler.http import setup_signal_handlers, shutdown_event

logger = logging.getLogger(__name__)


async def phase_discover(pool, max_pages=None, enrich_limit=None):
    """Phase 1: discover sites and enrich with API metadata."""
    from crawler import discover

    args = Namespace(
        max_pages=max_pages,
        start_page=1,
        enrich_limit=enrich_limit,
        browse_delay=1.0,
        api_delay=0.3,
    )

    logger.info("=== Phase 1a: Discovering sites ===")
    await discover.run_phase_1a(pool, args)

    if shutdown_event.is_set():
        return

    logger.info("=== Phase 1b: Enriching site metadata ===")
    await discover.run_phase_1b(pool, args)


async def phase_queue(pool):
    """Phase 2: build the crawl queue with skip logic."""
    logger.info("=== Phase 2: Building crawl queue ===")
    result = await db.build_crawl_queue(pool)
    logger.info(
        f"Queue built: {result['queued']} queued, "
        f"{result['skipped']} skipped, "
        f"{result['already_queued']} already queued"
    )


async def phase_crawl(pool, max_sites=None, max_pages_per_site=50, crawl_delay=1.5):
    """Phase 3: crawl sites and extract text."""
    from crawler import crawl

    args = Namespace(
        max_sites=max_sites,
        max_pages_per_site=max_pages_per_site,
        crawl_delay=crawl_delay,
    )

    logger.info("=== Phase 3: Crawling sites ===")
    await crawl.run_crawl(pool, args)


async def phase_index(pool, full_reindex=False):
    """Phase 4: build the Tantivy search index."""
    from search_api import indexer

    index_dir = os.environ.get("INDEX_DIR", "./data/index")

    if full_reindex:
        logger.info("=== Phase 4: Full reindex ===")
        count = await indexer.full_reindex(pool, index_dir)
    else:
        logger.info("=== Phase 4: Incremental index ===")
        count = await indexer.build_index(pool, index_dir)

    logger.info(f"Indexing complete: {count} documents")


async def run_pipeline(args):
    load_dotenv()

    pool = await db.create_pool()
    await db.init_schema(pool)

    try:
        phases = args.phases

        if phases in ("all", "discover"):
            await phase_discover(pool, max_pages=args.max_pages, enrich_limit=args.enrich_limit)
            if shutdown_event.is_set():
                return

        if phases in ("all", "queue"):
            await phase_queue(pool)
            if shutdown_event.is_set():
                return

        if phases in ("all", "crawl"):
            await phase_crawl(
                pool,
                max_sites=args.max_sites,
                max_pages_per_site=args.max_pages_per_site,
                crawl_delay=args.crawl_delay,
            )
            if shutdown_event.is_set():
                return

        if phases in ("all", "index"):
            await phase_index(pool, full_reindex=args.full_reindex)

        logger.info("=== Pipeline complete ===")

    finally:
        await pool.close()


def parse_args():
    parser = argparse.ArgumentParser(description="IndySearch Pipeline Orchestrator")
    parser.add_argument(
        "--phases", choices=["all", "discover", "queue", "crawl", "index"],
        default="all", help="Which phase(s) to run (default: all)",
    )
    parser.add_argument("--max-pages", type=int, default=None, help="Limit browse page scraping (discover phase)")
    parser.add_argument("--enrich-limit", type=int, default=None, help="Limit API enrichment (discover phase)")
    parser.add_argument("--max-sites", type=int, default=None, help="Limit sites to crawl (crawl phase)")
    parser.add_argument("--max-pages-per-site", type=int, default=50, help="Max pages per site (default: 50)")
    parser.add_argument("--crawl-delay", type=float, default=1.5, help="Seconds between crawl requests (default: 1.5)")
    parser.add_argument("--full-reindex", action="store_true", help="Force full index rebuild instead of incremental")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    setup_signal_handlers()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    asyncio.run(run_pipeline(args))
