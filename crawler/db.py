import os
import logging
from pathlib import Path

import asyncpg

logger = logging.getLogger(__name__)


async def create_pool() -> asyncpg.Pool:
    database_url = os.environ["DATABASE_URL"]
    return await asyncpg.create_pool(database_url, min_size=2, max_size=10)


async def init_schema(pool: asyncpg.Pool) -> None:
    schema_path = Path(__file__).parent / "schema.sql"
    sql = schema_path.read_text()
    async with pool.acquire() as conn:
        await conn.execute(sql)
    logger.info("Database schema initialized")


async def upsert_sites(pool: asyncpg.Pool, sites: list[dict]) -> int:
    """Bulk insert discovered sites. Returns count of newly inserted rows."""
    if not sites:
        return 0

    query = """
        INSERT INTO sites (sitename, url, custom_domain, tags, hits)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (sitename) DO NOTHING
    """
    inserted = 0
    async with pool.acquire() as conn:
        for site in sites:
            result = await conn.execute(
                query,
                site["sitename"],
                site["url"],
                site.get("custom_domain"),
                site.get("tags", []),
                site.get("hits", 0),
            )
            # asyncpg returns 'INSERT 0 1' on insert, 'INSERT 0 0' on conflict skip
            if result == "INSERT 0 1":
                inserted += 1

    return inserted


async def get_unenriched_sites(pool: asyncpg.Pool, limit: int = 1000) -> list[asyncpg.Record]:
    """Return sites not yet enriched with API metadata (created_at IS NULL)."""
    query = """
        SELECT sitename FROM sites
        WHERE created_at IS NULL
        ORDER BY id
        LIMIT $1
    """
    async with pool.acquire() as conn:
        return await conn.fetch(query, limit)


async def update_site_metadata(pool: asyncpg.Pool, sitename: str, metadata: dict) -> None:
    """Update a site row with API metadata."""
    query = """
        UPDATE sites
        SET hits = $2,
            tags = $3,
            created_at = $4,
            last_updated = $5,
            custom_domain = COALESCE($6, custom_domain)
        WHERE sitename = $1
    """
    async with pool.acquire() as conn:
        await conn.execute(
            query,
            sitename,
            metadata["hits"],
            metadata["tags"],
            metadata["created_at"],
            metadata["last_updated"],
            metadata.get("custom_domain"),
        )


async def get_discovery_stats(pool: asyncpg.Pool) -> dict:
    """Return discovery progress counts."""
    query = """
        SELECT
            COUNT(*) AS total,
            COUNT(*) FILTER (WHERE created_at IS NOT NULL) AS enriched,
            COUNT(*) FILTER (WHERE created_at IS NULL) AS unenriched
        FROM sites
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(query)
    return dict(row)


# ---------------------------------------------------------------------------
# Phase 2 — Crawl queue
# ---------------------------------------------------------------------------

async def build_crawl_queue(pool: asyncpg.Pool, dry_run: bool = False) -> dict:
    """Populate crawl_queue from sites table with skip logic.

    Returns dict with counts: queued, skipped, already_queued.
    """
    async with pool.acquire() as conn:
        # Get all enriched sites (have API metadata)
        sites = await conn.fetch("""
            SELECT id, url, has_been_indexed, last_indexed, last_updated
            FROM sites
            WHERE created_at IS NOT NULL
              AND crawl_allowed = TRUE
            ORDER BY id
        """)

        # Get site_ids already in queue (to avoid duplicates)
        existing = set()
        rows = await conn.fetch("SELECT site_id FROM crawl_queue")
        for r in rows:
            existing.add(r["site_id"])

        queued = 0
        skipped = 0
        already_queued = 0

        for site in sites:
            if site["id"] in existing:
                already_queued += 1
                continue

            has_been_indexed = site["has_been_indexed"]
            last_indexed = site["last_indexed"]
            last_updated = site["last_updated"]

            # Skip logic: if site hasn't changed since last crawl, skip
            if has_been_indexed and last_updated and last_indexed and last_updated < last_indexed:
                status = "skipped"
                skipped += 1
            else:
                status = "pending"
                queued += 1

            if not dry_run:
                await conn.execute("""
                    INSERT INTO crawl_queue (site_id, url, has_been_indexed, last_indexed, last_updated, status)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    ON CONFLICT (site_id) DO NOTHING
                """, site["id"], site["url"], has_been_indexed, last_indexed, last_updated, status)

    return {"queued": queued, "skipped": skipped, "already_queued": already_queued}


async def get_next_queued_sites(pool: asyncpg.Pool, limit: int = 10) -> list[asyncpg.Record]:
    """Fetch pending sites from crawl queue, ordered by priority."""
    query = """
        SELECT cq.site_id, cq.url, s.sitename, s.tags, s.hits, s.last_updated
        FROM crawl_queue cq
        JOIN sites s ON s.id = cq.site_id
        WHERE cq.status = 'pending'
        ORDER BY cq.priority DESC, cq.site_id
        LIMIT $1
    """
    async with pool.acquire() as conn:
        return await conn.fetch(query, limit)


async def update_queue_status(pool: asyncpg.Pool, site_id: int, status: str) -> None:
    """Update a queue entry's status."""
    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE crawl_queue
            SET status = $2, last_attempt = NOW(), attempts = attempts + 1
            WHERE site_id = $1
        """, site_id, status)


async def mark_site_crawled(pool: asyncpg.Pool, site_id: int, page_count: int) -> None:
    """Mark a site as fully crawled and indexed."""
    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE sites
            SET has_been_indexed = TRUE,
                last_indexed = NOW(),
                page_count = $2,
                status = 'indexed'
            WHERE id = $1
        """, site_id, page_count)
        await conn.execute("""
            UPDATE crawl_queue
            SET status = 'done', last_crawled = NOW()
            WHERE site_id = $1
        """, site_id)


async def mark_site_blocked(pool: asyncpg.Pool, site_id: int) -> None:
    """Mark a site as blocked by robots.txt."""
    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE sites SET crawl_allowed = FALSE, status = 'blocked' WHERE id = $1
        """, site_id)
        await conn.execute("""
            UPDATE crawl_queue SET status = 'done' WHERE site_id = $1
        """, site_id)


# ---------------------------------------------------------------------------
# Phase 3 — Documents
# ---------------------------------------------------------------------------

async def upsert_document(pool: asyncpg.Pool, doc: dict) -> None:
    """Insert or update a document row."""
    query = """
        INSERT INTO documents (site_id, url, path, title, body_snippet, description, author, word_count, r2_key)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
        ON CONFLICT (site_id, path) DO UPDATE SET
            url = EXCLUDED.url,
            title = EXCLUDED.title,
            body_snippet = EXCLUDED.body_snippet,
            description = EXCLUDED.description,
            author = EXCLUDED.author,
            word_count = EXCLUDED.word_count,
            r2_key = EXCLUDED.r2_key,
            extracted_at = NOW(),
            indexed = FALSE
    """
    async with pool.acquire() as conn:
        await conn.execute(
            query,
            doc["site_id"],
            doc["url"],
            doc["path"],
            doc.get("title"),
            doc.get("body_snippet"),
            doc.get("description"),
            doc.get("author"),
            doc.get("word_count", 0),
            doc.get("r2_key"),
        )


async def get_crawl_stats(pool: asyncpg.Pool) -> dict:
    """Return crawl progress counts."""
    async with pool.acquire() as conn:
        queue_row = await conn.fetchrow("""
            SELECT
                COUNT(*) FILTER (WHERE status = 'pending') AS pending,
                COUNT(*) FILTER (WHERE status = 'in_progress') AS in_progress,
                COUNT(*) FILTER (WHERE status = 'done') AS done,
                COUNT(*) FILTER (WHERE status = 'skipped') AS skipped,
                COUNT(*) FILTER (WHERE status = 'failed') AS failed
            FROM crawl_queue
        """)
        doc_count = await conn.fetchval("SELECT COUNT(*) FROM documents")
    result = dict(queue_row)
    result["documents"] = doc_count
    return result


async def record_crawl_stats(pool: asyncpg.Pool, sites_crawled: int, pages_indexed: int) -> None:
    """Snapshot crawl session stats into crawl_stats table."""
    async with pool.acquire() as conn:
        total_sites = await conn.fetchval(
            "SELECT COUNT(*) FROM sites WHERE has_been_indexed = TRUE"
        )
        total_documents = await conn.fetchval("SELECT COUNT(*) FROM documents")
        await conn.execute("""
            INSERT INTO crawl_stats (sites_crawled, pages_indexed, total_sites, total_documents)
            VALUES ($1, $2, $3, $4)
        """, sites_crawled, pages_indexed, total_sites, total_documents)
    logger.info(f"Recorded crawl stats: {sites_crawled} sites, {pages_indexed} pages")


async def get_crawl_history(pool: asyncpg.Pool, limit: int = 50) -> list[asyncpg.Record]:
    """Return recent crawl snapshots, newest first."""
    async with pool.acquire() as conn:
        return await conn.fetch("""
            SELECT completed_at, sites_crawled, pages_indexed, total_sites, total_documents
            FROM crawl_stats
            ORDER BY completed_at DESC
            LIMIT $1
        """, limit)
