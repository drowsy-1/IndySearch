import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg
import tantivy
from dotenv import load_dotenv
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from tantivy import SnippetGenerator

from search_api import indexer

load_dotenv()
logger = logging.getLogger(__name__)

INDEX_DIR = os.environ.get("INDEX_DIR", "./data/index")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    index_path = Path(INDEX_DIR)
    meta_file = index_path / "meta.json"
    if not meta_file.exists():
        logger.warning(f"No index found at {INDEX_DIR}. Run the crawler pipeline first.")
        app.state.index = None
    else:
        app.state.index = tantivy.Index.open(str(index_path))
        app.state.index.reload()
        logger.info(f"Search index loaded from {INDEX_DIR}")

    database_url = os.environ.get("DATABASE_URL")
    if database_url:
        app.state.pool = await asyncpg.create_pool(database_url, min_size=1, max_size=5)
    else:
        app.state.pool = None

    yield

    if app.state.pool:
        await app.state.pool.close()


app = FastAPI(title="IndySearch API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://indysearch.neocities.org",
        "http://localhost:3000",
        "http://localhost:5500",
        "http://localhost:8080",
        "http://127.0.0.1:5500",
        "http://127.0.0.1:8080",
        "null",
    ],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class SearchHit(BaseModel):
    score: float
    doc_id: int | None = None
    title: str | None = None
    url: str | None = None
    sitename: str | None = None
    snippet: str | None = None
    tags: list[str] = []
    word_count: int = 0
    hits: int = 0


class SearchResponse(BaseModel):
    count: int
    query: str
    hits: list[SearchHit]


class ReindexResponse(BaseModel):
    status: str
    documents_indexed: int


class CrawlSnapshot(BaseModel):
    completed_at: str
    sites_crawled: int
    pages_indexed: int
    total_sites: int
    total_documents: int


class StatsResponse(BaseModel):
    index_loaded: bool
    index_dir: str
    num_docs: int | None = None
    last_crawl: str | None = None
    crawl_history: list[CrawlSnapshot] = []


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/search", response_model=SearchResponse)
def search(
    q: str = Query(..., min_length=1, max_length=200),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    index = app.state.index
    if index is None:
        return SearchResponse(count=0, query=q, hits=[])

    query = index.parse_query(
        q,
        default_field_names=["title", "body", "sitename", "tags"],
        field_boosts={"title": 3.0, "sitename": 2.0, "tags": 1.5, "body": 1.0},
    )

    searcher = index.searcher()
    search_result = searcher.search(query, limit=limit, count=True, offset=offset)

    snippet_gen = SnippetGenerator.create(searcher, query, index.schema, "body")

    hits = []
    for score, doc_address in search_result.hits:
        doc = searcher.doc(doc_address)
        snippet = snippet_gen.snippet_from_doc(doc)

        tags = doc.get_all("tags") if hasattr(doc, "get_all") else []

        hits.append(SearchHit(
            score=round(score, 4),
            doc_id=doc.get_first("doc_id"),
            title=doc.get_first("title"),
            url=doc.get_first("url"),
            sitename=doc.get_first("sitename"),
            snippet=snippet.to_html(),
            tags=tags,
            word_count=doc.get_first("word_count") or 0,
            hits=doc.get_first("hits") or 0,
        ))

    return SearchResponse(count=search_result.count, query=q, hits=hits)


@app.post("/admin/reindex", response_model=ReindexResponse)
async def admin_reindex():
    pool = app.state.pool
    if not pool:
        return ReindexResponse(status="error: no database connection", documents_indexed=0)

    count = await indexer.build_index(pool, INDEX_DIR)

    # Reload index so new searchers see the updated documents
    index_path = Path(INDEX_DIR)
    if index_path.exists() and any(index_path.iterdir()):
        app.state.index = tantivy.Index.open(str(index_path))
        app.state.index.reload()

    return ReindexResponse(status="ok", documents_indexed=count)


@app.get("/admin/stats", response_model=StatsResponse)
async def admin_stats():
    index = app.state.index
    num_docs = None
    if index is not None:
        searcher = index.searcher()
        num_docs = searcher.num_docs

    last_crawl = None
    crawl_history = []

    pool = app.state.pool
    if pool:
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT completed_at, sites_crawled, pages_indexed, total_sites, total_documents
                FROM crawl_stats
                ORDER BY completed_at DESC
                LIMIT 50
            """)
            if rows:
                last_crawl = rows[0]["completed_at"].strftime("%B %Y")
                crawl_history = [
                    CrawlSnapshot(
                        completed_at=r["completed_at"].isoformat(),
                        sites_crawled=r["sites_crawled"],
                        pages_indexed=r["pages_indexed"],
                        total_sites=r["total_sites"],
                        total_documents=r["total_documents"],
                    ) for r in rows
                ]

    return StatsResponse(
        index_loaded=index is not None,
        index_dir=INDEX_DIR,
        num_docs=num_docs,
        last_crawl=last_crawl,
        crawl_history=crawl_history,
    )
