import argparse
import asyncio
import logging

from dotenv import load_dotenv

from crawler import db
from crawler.http import setup_signal_handlers

logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="IndySearch Phase 2: Build Crawl Queue")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be queued without writing")
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
        if args.dry_run:
            logger.info("DRY RUN — no changes will be written")

        stats = await db.build_crawl_queue(pool, dry_run=args.dry_run)
        logger.info(
            f"Queue build complete. "
            f"Queued: {stats['queued']} | "
            f"Skipped (unchanged): {stats['skipped']} | "
            f"Already in queue: {stats['already_queued']}"
        )

        if not args.dry_run:
            crawl_stats = await db.get_crawl_stats(pool)
            logger.info(
                f"Queue totals — "
                f"Pending: {crawl_stats['pending']} | "
                f"Done: {crawl_stats['done']} | "
                f"Skipped: {crawl_stats['skipped']} | "
                f"Failed: {crawl_stats['failed']}"
            )
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main(parse_args()))
