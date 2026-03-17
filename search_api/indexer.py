import argparse
import asyncio
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import asyncpg
import tantivy
from dotenv import load_dotenv

from crawler import storage

logger = logging.getLogger(__name__)

COMMIT_BATCH_SIZE = 10_000


def create_schema() -> tantivy.Schema:
    builder = tantivy.SchemaBuilder()
    builder.add_unsigned_field("doc_id", stored=True, indexed=True, fast=True)
    builder.add_unsigned_field("site_id", stored=True, indexed=True, fast=True)
    builder.add_text_field("title", stored=True, tokenizer_name="en_stem")
    builder.add_text_field("url", stored=True, tokenizer_name="raw")
    builder.add_text_field("sitename", stored=True, tokenizer_name="en_stem")
    builder.add_text_field("body", stored=True, tokenizer_name="en_stem")
    builder.add_text_field("tags", stored=True, tokenizer_name="default")
    builder.add_unsigned_field("word_count", stored=True, fast=True)
    builder.add_unsigned_field("hits", stored=True, fast=True)
    builder.add_date_field("last_updated", stored=True, indexed=True, fast=True)
    return builder.build()


def open_or_create_index(index_path: str) -> tantivy.Index:
    path = Path(index_path)
    path.mkdir(parents=True, exist_ok=True)
    schema = create_schema()
    return tantivy.Index(schema, path=str(path), reuse=True)


async def get_unindexed_documents(pool: asyncpg.Pool, limit: int = 50_000) -> list[asyncpg.Record]:
    query = """
        SELECT d.id, d.site_id, d.url, d.title, d.word_count, d.r2_key,
               s.sitename, s.tags, s.hits, s.last_updated
        FROM documents d
        JOIN sites s ON s.id = d.site_id
        WHERE d.indexed = FALSE
        ORDER BY d.id
        LIMIT $1
    """
    async with pool.acquire() as conn:
        return await conn.fetch(query, limit)


async def mark_documents_indexed(pool: asyncpg.Pool, doc_ids: list[int]) -> None:
    if not doc_ids:
        return
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE documents SET indexed = TRUE WHERE id = ANY($1::int[])",
            doc_ids,
        )


async def build_index(pool: asyncpg.Pool, index_path: str) -> int:
    """Add unindexed documents to the Tantivy index. Returns count indexed."""
    index = open_or_create_index(index_path)
    writer = index.writer(heap_size=256_000_000, num_threads=1)

    total_indexed = 0
    indexed_ids = []

    while True:
        docs = await get_unindexed_documents(pool)
        if not docs:
            break

        batch_ids = []
        for row in docs:
            r2_key = row["r2_key"]
            if not r2_key:
                logger.warning(f"Document {row['id']} has no r2_key, skipping")
                batch_ids.append(row["id"])
                continue

            try:
                body = storage.load_text(r2_key)
            except FileNotFoundError:
                logger.warning(f"Text not found for doc {row['id']} at {r2_key}, skipping")
                batch_ids.append(row["id"])
                continue

            last_updated = row["last_updated"]
            if last_updated and last_updated.tzinfo is None:
                last_updated = last_updated.replace(tzinfo=timezone.utc)

            doc = tantivy.Document(
                title=[row["title"] or ""],
                url=[row["url"]],
                sitename=[row["sitename"] or ""],
                body=[body],
            )
            doc.add_unsigned("doc_id", row["id"])
            doc.add_unsigned("site_id", row["site_id"])
            doc.add_unsigned("word_count", row["word_count"] or 0)
            doc.add_unsigned("hits", row["hits"] or 0)

            # Add tags individually (multi-valued field)
            tags = row["tags"] or []
            for tag in tags:
                doc.add_text("tags", tag)

            if last_updated:
                doc.add_date("last_updated", last_updated)

            writer.add_document(doc)
            batch_ids.append(row["id"])
            total_indexed += 1

        # Commit and mark indexed after each DB batch
        writer.commit()
        await mark_documents_indexed(pool, batch_ids)
        logger.info(f"Committed batch, {total_indexed} documents total")

    writer.wait_merging_threads()
    index.reload()

    logger.info(f"Indexing complete. {total_indexed} documents indexed.")
    return total_indexed


async def full_reindex(pool: asyncpg.Pool, index_path: str) -> int:
    """Delete all index data and rebuild from scratch."""
    # Reset indexed flag in PostgreSQL
    async with pool.acquire() as conn:
        await conn.execute("UPDATE documents SET indexed = FALSE")
    logger.info("Reset all documents to unindexed")

    # Delete existing index files and recreate
    path = Path(index_path)
    if path.exists():
        import shutil
        shutil.rmtree(path)
        logger.info(f"Deleted existing index at {path}")

    return await build_index(pool, index_path)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="IndySearch: Build Tantivy Index")
    parser.add_argument("--mode", choices=["incremental", "full"], default="incremental",
                        help="incremental = add unindexed docs; full = rebuild everything")
    parser.add_argument("--index-dir", default=None, help="Tantivy index directory (default: INDEX_DIR env or ./data/index)")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser.parse_args()


async def main(args: argparse.Namespace) -> None:
    load_dotenv()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    index_dir = args.index_dir or os.environ.get("INDEX_DIR", "./data/index")
    database_url = os.environ["DATABASE_URL"]

    pool = await asyncpg.create_pool(database_url, min_size=2, max_size=5)
    try:
        if args.mode == "full":
            count = await full_reindex(pool, index_dir)
        else:
            count = await build_index(pool, index_dir)
        logger.info(f"Done. {count} documents in index.")
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main(parse_args()))
