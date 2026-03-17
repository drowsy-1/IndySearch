# Neocities Search Engine

> A search engine for the indie web — crawling, indexing, and searching ~800K+ static sites hosted on Neocities.org, with the frontend hosted on Neocities itself.

---

## Project Description

Neocities is the spiritual successor to GeoCities: a free static web hosting platform where people build hand-crafted personal websites using HTML, CSS, and JavaScript. As of late 2025, it hosts over 1.3 million created sites, with roughly 358,000+ actively maintained. There is no native full-text search across these sites — if you want to find something on the indie web, you're limited to Neocities' browse page (sorted by followers/date) or hoping Google indexed it.

This project builds a dedicated search engine for the Neocities ecosystem. It discovers sites through the platform's browse page, crawls their public HTML with ethical rate-limiting, extracts clean text, and indexes it for fast full-text search with BM25 ranking. The frontend is a static site hosted on Neocities itself — a search engine for Neocities, on Neocities.

The backend infrastructure runs entirely on Railway, leveraging its persistent services, cron scheduling, managed PostgreSQL, and persistent volumes. Cloudflare R2 provides cold storage for compressed HTML archives at near-zero cost.

---

## Goals

### Primary

- **Full-text search across Neocities** — users enter a query, get ranked results from hundreds of thousands of indie web pages with sub-100ms latency
- **Ethical crawling** — respect robots.txt, use honest User-Agent identification, maintain polite rate limits (1-2s between requests), and provide opt-out mechanisms for site owners
- **Low operational cost** — target $10-20/month total by leveraging Railway's usage-based billing, R2's zero-egress pricing, and Neocities' free hosting tier

### Secondary

- **Freshness** — incremental re-crawling via cron to detect updated sites, prioritized by the Neocities API's `last_updated` field
- **Indie web ranking signals** — go beyond BM25 text relevance by incorporating Neocities-specific metadata: hit counts (popularity), tags (categorization), inter-site links (community graph), content freshness, and word count thresholds (filter abandoned/test pages)
- **Community value** — build something the Neocities community would want to exist, with transparency about what's indexed and how
- **Storing Image Meta** — store image meta descritions and path so those can be searchable

### Non-Goals (for now)

- Real-time indexing (batch processing is sufficient given how infrequently most Neocities sites update)
- Image search or media indexing (text-only initially)
- Replacing Google/Bing for Neocities sites (complementary, not competitive)

---

## Architecture Overview

```
┌──────────────────────────────────────────────────────────────┐
│  Neocities (yoursearch.neocities.org)                        │
│  Static frontend: HTML + CSS + JS                            │
│  fetch() ──────────────────────────────────────┐             │
└────────────────────────────────────────────────┼─────────────┘
                                                 │
                                                 ▼
┌──────────────────────────────────────────────────────────────┐
│  Railway Project                                             │
│                                                              │
│  ┌─────────────────────────────────────────────────────┐     │
│  │  search-api (persistent service, public domain)     │     │
│  │  FastAPI + tantivy-py                               │     │
│  │  Volume: /data/index (Tantivy index, ~1-2 GB)       │     │
│  │  GET /search?q=...                                  │     │
│  │  POST /admin/reindex (internal network only)        │     │
│  └────────────────────────┬────────────────────────────┘     │
│                           │ reads from                       │
│  ┌────────────────────────▼────────────────────────────┐     │
│  │  PostgreSQL (Railway managed)                       │     │
│  │  - sites: catalog, metadata, indexing state         │     │
│  │  - documents: doc metadata + R2 refs (no body text) │     │
│  │  - crawl_queue: queue with skip logic columns       │     │
│  └────────────────────────▲────────────────────────────┘     │
│                           │ writes to                        │
│  ┌────────────────────────┴────────────────────────────┐     │
│  │  crawler-pipeline (cron service)                    │     │
│  │  Python: httpx + BeautifulSoup + trafilatura        │     │
│  │  Phase 1: Discover all domains (full list first)    │     │
│  │  Phase 2: Build crawl queue (skip unchanged sites)  │     │
│  │  Phase 3: Crawl → Extract → Strip HTML → Compress   │     │
│  │  Phase 4: Index + trigger reindex via internal net  │     │
│  └─────────────────────────────────────────────────────┘     │
│                                                              │
└──────────────────────────────────────────────────────────────┘
                         │
                         │ stores compressed parsed text only
                         ▼
              ┌──────────────────────────┐
              │  Cloudflare R2           │
              │  Long-term storage       │
              │  zstd-compressed         │
              │  extracted text (no HTML) │
              └──────────────────────────┘
```

### Data Flow — Procedural Pipeline

The crawler follows a strict procedural order. Each phase must complete before the next begins.

#### Phase 1: Discovery (domain list assembly)

1. **Scrape browse pages** — Walk all Neocities browse pages to collect every sitename
2. **Build domain list** — Construct the home URL for each site (`https://{sitename}.neocities.org`)
3. **Deduplicate** — Remove duplicate home pages (sites reachable via custom domain + `*.neocities.org` alias, etc.)
4. **Enrich with API metadata** — Call `/api/info?sitename={name}` for each site to populate `hits`, `tags`, `created_at`, `last_updated`
5. **Persist the complete site list** — Write all discovered sites to the `sites` table before any crawling begins

At the end of Phase 1, the database contains a full catalog of every known Neocities domain with metadata — but no content has been downloaded yet.

#### Phase 2: Crawl queue creation

6. **Build the crawl queue** — For each site in the catalog, create a queue entry with tracking columns:
   - `has_been_indexed` (BOOLEAN) — whether this site has ever been crawled and indexed
   - `last_indexed` (TIMESTAMPTZ) — when it was last successfully crawled and indexed
   - `last_updated` (TIMESTAMPTZ) — the site's `last_updated` value from the Neocities API
7. **First crawl: all sites queued** — On the initial run, every site has `has_been_indexed = FALSE`, so every site enters the queue
8. **Subsequent crawls: conditional skip** — On incremental runs after the initial discovery:
   - If `has_been_indexed = TRUE`, check the Neocities API for the site's current `last_updated` timestamp
   - If `last_updated` is **before** `last_indexed`, the site hasn't changed since we last indexed it — **skip this iteration**
   - If `last_updated` is **on or after** `last_indexed`, the site has been updated — add to the crawl queue
   - If `has_been_indexed = FALSE` (newly discovered site), always add to the queue

#### Phase 3: Crawling & extraction

9. **robots.txt check** — Before crawling each site, fetch and parse its `robots.txt`; mark blocked sites as `crawl_allowed = FALSE` and skip them
10. **Fetch HTML** — Download pages from queued sites with ethical rate limiting (1-2s delay between requests)
11. **Discover sub-pages** — Parse each page's HTML for internal links; add discovered sub-page URLs to the crawl queue for that site (up to 50 pages per site)
12. **Extract text** — Strip HTML structure with trafilatura (`favor_recall=True`) to produce clean text + metadata; discard the raw HTML after extraction
13. **Compress and store parsed text** — Compress the extracted text with zstd (dictionary-trained) and store only the compressed parsed text to R2 for long-term storage; raw HTML is not retained

#### Phase 4: Indexing & serving

14. **Write to PostgreSQL** — Store extracted document metadata (title, URL, path, word count, etc.) in the `documents` table; the `body` column holds a reference to the compressed text in R2 or is populated on demand
15. **Build Tantivy index** — Index all unindexed documents into the Tantivy inverted index on the search API's persistent volume
16. **Update queue state** — Set `has_been_indexed = TRUE` and `last_indexed = NOW()` for all successfully processed sites
17. **Serve** — Static JS frontend on Neocities sends queries to the Railway-hosted FastAPI search API

---

## Technology Stack

### Frontend

| Component | Technology | Role |
|-----------|-----------|------|
| **Hosting** | Neocities (free tier) | Static file hosting — 1 GB storage, 200 GB bandwidth |
| **Framework** | Vanilla HTML/CSS/JS | No build step, no dependencies — fits Neocities' ethos |
| **Deployment** | Neocities CLI or API | `neocities push build/` or POST to `/api/upload` |

**Why Neocities for the frontend:** It's philosophically appropriate — a search engine for Neocities, hosted on Neocities. The free tier is more than sufficient for a search interface (a few KB of HTML/CSS/JS). No server-side execution is needed; all logic is a `fetch()` call to the search API.

**Allowed file types (free tier):** HTML, CSS, JavaScript, Markdown, XML, text, fonts, images. No server-side scripting.

### Backend — Railway Services

| Service | Type | Technology | Role |
|---------|------|-----------|------|
| **search-api** | Persistent (always-on) | Python, FastAPI, tantivy-py | Public search endpoint; owns the Tantivy index |
| **crawler-pipeline** | Cron (scheduled) | Python, httpx, BeautifulSoup4, trafilatura | Site discovery, HTML crawling, text extraction |
| **PostgreSQL** | Managed database | PostgreSQL (Railway one-click) | Site catalog, extracted documents, crawl state |

**Why Railway:**

- Already in use for Hemeroholics backend — no new vendor, no new billing
- Usage-based pricing means the search API costs almost nothing when idle between queries (pay for actual CPU/memory utilization, not reserved capacity)
- Native cron job support — the crawler wakes on schedule, runs, exits, and stops billing
- Managed PostgreSQL with backups — no manual database administration
- Internal networking between services (crawler triggers reindex on search-api without public exposure)
- Persistent volumes for the Tantivy index — SSD-backed, survives deploys
- Pro plan includes $20/month in usage credits, which likely covers this entire project

### Search Engine

| Component | Technology | Role |
|-----------|-----------|------|
| **Full-text index** | **Tantivy** (via tantivy-py) | Inverted index with BM25 scoring, phrase search, fuzzy matching |
| **API layer** | **FastAPI** | Async Python web framework wrapping Tantivy queries |
| **Storage** | Railway Volume (SSD) | Persistent mount for the Tantivy index files (~1-2 GB) |

**Why Tantivy:**

Tantivy is a Rust full-text search library modeled after Apache Lucene, with Python bindings via tantivy-py. It was selected over alternatives (Meilisearch, SQLite FTS5, Elasticsearch) for this combination of properties:

- **Compression efficiency** — Partitioned Elias-Fano encoding for posting lists (near information-theoretic optimum for sorted integer sequences); FST-based term dictionaries (extremely compact, support prefix iteration for autocomplete)
- **Query performance** — Block-Max WAND algorithm for early termination (examines <1% of documents for common terms); sub-millisecond P99 latency for 10-term queries
- **Indexing throughput** — ~12,500 docs/sec single-threaded; the full 800K-site corpus indexes in under 30 minutes
- **Index size** — typically 20-30% of raw text size; 4 GB of extracted text produces a ~0.8-1.2 GB index
- **Library, not a service** — runs in-process with the FastAPI app; no separate search server to manage, no network hop for queries
- **Feature set** — field-level search (weight title above body), phrase queries, fuzzy search via Levenshtein automata over FSTs, boolean queries, range filters on numeric/date fields, stored fields for result display
- **Memory-mapped I/O** — loads index by mmapping files; very low anonymous memory usage; the OS page cache handles hot segments

**Tokenizers:**

- `en_stem` for title and body fields (lowercase → split on punctuation/whitespace → English stemming via Porter algorithm)
- `raw` for URL and sitename fields (exact match, no tokenization)

**Scoring:** BM25 by default, with planned custom scoring to incorporate Neocities-specific signals (hit count, freshness, link graph).

### Crawling & Extraction

| Component | Technology | Role |
|-----------|-----------|------|
| **HTTP client** | **httpx** (async) | Async HTTP requests with connection pooling, timeouts, redirect following |
| **HTML parsing** | **BeautifulSoup4** | Parse browse pages for site discovery; extract internal links during crawling |
| **robots.txt** | **urllib.robotparser** (stdlib) | Check each site's robots.txt before crawling |
| **Content extraction** | **trafilatura** | Strip HTML boilerplate → clean text + metadata |
| **Scheduling** | Railway Cron | `0 3 * * *` (daily at 3 AM UTC) for incremental re-crawls |

**Why trafilatura:**

Trafilatura is purpose-built for extracting article/page content from HTML while stripping navigation, footers, scripts, styles, and boilerplate. It was selected over alternatives (readability, newspaper3k, raw BeautifulSoup) because:

- **Best-in-class extraction quality** for diverse, non-standard HTML — critical for Neocities sites that use unconventional markup (90s/2000s-era patterns, no semantic `<article>` tags, table-based layouts)
- **70-90% size reduction** from raw HTML to clean text — a 10 KB HTML page typically yields 1-3 KB of extracted text
- **Built-in metadata extraction** — title, author, date, description, and site name parsed automatically
- **Multiple extraction strategies** with fallback chain — if the primary algorithm fails on quirky HTML, it falls back to readability heuristics, then baseline text extraction
- **`favor_recall=True` mode** — casts a wider net for content, essential for personal websites where the "main content" is often the entire page rather than a clearly delineated article body
- **Output format flexibility** — plain text, JSON (with inline metadata), XML (preserving structure), Markdown
- **Language detection** — can filter to English-only or other target languages if needed

**Crawling ethics:**

- User-Agent: `NeocitiesSearch/1.0 (+https://yoursearch.neocities.org/about)`
- Delay: 1-2 seconds between requests to any single site
- robots.txt: checked and honored for every site before crawling
- Neocities API restriction: the API explicitly prohibits bulk data mining — site discovery uses the public browse page instead, and content crawling uses direct HTTP requests to public `*.neocities.org` URLs
- Opt-out: site owners can block the crawler via their robots.txt
- No proxies: transparent single-IP crawling with honest identification

### Storage & Compression

| Component | Technology | Role |
|-----------|-----------|------|
| **Hot storage** | **Railway PostgreSQL** | Extracted documents, site metadata, crawl state — everything the search API and crawler need at low latency |
| **Index storage** | **Railway Volume** (SSD) | Tantivy index files; persistent across deploys; mmap-friendly |
| **Cold storage** | **Cloudflare R2** | Compressed parsed text for long-term storage, snippet generation, and reprocessing |
| **Compression** | **zstd** (Zstandard) with trained dictionary | Per-document compression of extracted text only (raw HTML is discarded after extraction) |

**Why zstd with dictionary training:**

Standard compression algorithms perform poorly on small files (1-10 KB) because they can't build enough context. Extracted text from Neocities pages shares structural patterns — common words, similar phrasing, metadata fields — that a trained dictionary captures. The dictionary "pre-loads" these patterns so even a 2 KB text document compresses well.

- Train a 64 KB dictionary on a sample of 1,000-10,000 extracted text documents
- Compress individual documents at level 15-19 (high ratio; decompression is fast at any level)
- **Improvement over non-dictionary compression:** ~15-25% better ratios for small documents
- **Decompression speed:** ~1.5 GB/s regardless of compression level — essentially free at query time
- **Total compressed corpus:** ~4-17 GB extracted text compresses to ~0.8-3 GB with dictionary
- **Raw HTML is NOT stored** — only the parsed text output from trafilatura is compressed and retained; this dramatically reduces long-term storage requirements

**Why R2 for cold storage:**

- **Zero egress fees** — the killer feature; the search API can read archived documents from R2 without transfer costs
- **S3-compatible API** — use boto3 or any S3 SDK; no vendor-specific client
- **Free tier:** 10 GB storage, 1M Class A (write) ops, 10M Class B (read) ops per month
- **Paid:** $0.015/GB/month storage — the full compressed archive costs pennies
- **Use case:** compressed parsed text storage for generating search result snippets on demand and reprocessing if the indexing pipeline changes; raw HTML is not retained to minimize long-term storage costs

**Why PostgreSQL (not SQLite) for operational data:**

- Railway provides managed Postgres with automated backups — zero administration
- The crawler and search API are separate Railway services; they need a shared database accessible over the internal network (SQLite is single-process)
- Postgres handles concurrent reads (search API) and writes (crawler) without locking issues
- JSONB columns for flexible metadata storage (tags, extraction results)
- Full-text search as a fallback if Tantivy is temporarily unavailable

### Deployment & CI/CD

| Component | Technology | Role |
|-----------|-----------|------|
| **Backend deploy** | Railway (GitHub integration) | Push to main → auto-deploy all services |
| **Frontend deploy** | Neocities CLI or GitHub Actions | `neocities push build/` or automated via `deploy-to-neocities` action |
| **Environment config** | Railway Variables | API keys, database URLs, R2 credentials — shared across services |

**Railway project structure:**

```
Railway Project: neocities-search
│
├── search-api/              (persistent service, public domain)
│   ├── Dockerfile
│   ├── main.py              (FastAPI app)
│   ├── indexer.py            (Tantivy index management)
│   └── Volume: /data/index
│
├── crawler-pipeline/        (cron: "0 3 * * *")
│   ├── Dockerfile
│   ├── discover.py           (Phase 1: browse page scraper → full domain list)
│   ├── queue.py              (Phase 2: build crawl queue with skip logic)
│   ├── crawl.py              (Phase 3: async HTTP crawler + sub-page discovery)
│   ├── extract.py            (Phase 3: trafilatura pipeline → text only)
│   └── store.py              (Phase 3: zstd compress parsed text → R2 upload)
│
└── PostgreSQL               (Railway managed)
    ├── sites
    ├── documents
    └── crawl_queue
```

**Internal networking:** The crawler triggers the search API's reindex endpoint via Railway's private network (`http://search-api.railway.internal/admin/reindex`) — no public exposure, no authentication overhead for internal calls.

---

## Database Schema (PostgreSQL)

```sql
-- Site catalog: one row per Neocities site
-- Populated during Phase 1 (Discovery) before any crawling begins
CREATE TABLE sites (
    id                SERIAL PRIMARY KEY,
    sitename          TEXT UNIQUE NOT NULL,
    url               TEXT NOT NULL,              -- https://{sitename}.neocities.org
    custom_domain     TEXT,                       -- from API, null if none
    hits              INTEGER DEFAULT 0,          -- from API /info
    tags              TEXT[] DEFAULT '{}',         -- from API /info
    created_at        TIMESTAMPTZ,               -- from API /info
    last_updated      TIMESTAMPTZ,               -- from API /info (used for skip logic)
    discovered_at     TIMESTAMPTZ DEFAULT NOW(),
    has_been_indexed  BOOLEAN DEFAULT FALSE,      -- has this site ever been crawled & indexed?
    last_indexed      TIMESTAMPTZ,               -- when was this site last successfully indexed?
    crawl_allowed     BOOLEAN DEFAULT TRUE,       -- robots.txt result
    page_count        INTEGER DEFAULT 0,
    status            TEXT DEFAULT 'discovered'   -- discovered | queued | crawling | indexed | failed | blocked
);

-- Extracted documents: one row per crawled page
CREATE TABLE documents (
    id              SERIAL PRIMARY KEY,
    site_id         INTEGER REFERENCES sites(id),
    url             TEXT NOT NULL,
    path            TEXT NOT NULL,            -- /index.html, /about.html, etc.
    title           TEXT,
    description     TEXT,
    author          TEXT,
    word_count      INTEGER DEFAULT 0,
    extracted_at    TIMESTAMPTZ DEFAULT NOW(),
    indexed         BOOLEAN DEFAULT FALSE,    -- has this been added to Tantivy?
    r2_key          TEXT,                     -- compressed parsed text key in R2 (no raw HTML stored)
    UNIQUE(site_id, path)
);
-- Note: the body text is NOT stored in PostgreSQL. It is compressed with zstd
-- and stored in R2 (referenced by r2_key) to save on database storage costs.
-- The Tantivy index holds the searchable text; R2 holds the compressed text
-- for snippet generation and reprocessing.

-- Crawl queue: built during Phase 2, consumed during Phase 3
CREATE TABLE crawl_queue (
    id              SERIAL PRIMARY KEY,
    site_id         INTEGER REFERENCES sites(id) UNIQUE,
    url             TEXT NOT NULL,
    has_been_indexed BOOLEAN DEFAULT FALSE,   -- mirrors sites.has_been_indexed at queue build time
    last_indexed    TIMESTAMPTZ,             -- mirrors sites.last_indexed at queue build time
    last_updated    TIMESTAMPTZ,             -- from API: when was the site last modified?
    priority        INTEGER DEFAULT 0,       -- higher = crawl sooner
    attempts        INTEGER DEFAULT 0,
    last_attempt    TIMESTAMPTZ,
    status          TEXT DEFAULT 'pending'   -- pending | in_progress | done | skipped | failed
);
-- Skip logic (Phase 2):
--   IF has_been_indexed = TRUE
--     AND last_updated < last_indexed
--   THEN status = 'skipped' (site unchanged since last index, skip this iteration)

CREATE INDEX idx_sites_status ON sites(status);
CREATE INDEX idx_sites_last_updated ON sites(last_updated DESC);
CREATE INDEX idx_sites_has_been_indexed ON sites(has_been_indexed);
CREATE INDEX idx_documents_indexed ON documents(indexed) WHERE NOT indexed;
CREATE INDEX idx_crawl_queue_pending ON crawl_queue(priority DESC) WHERE status = 'pending';
```

---

## Neocities API Reference (Search-Engine-Relevant Subset)

**Base URL:** `https://neocities.org/api`

### GET /api/info?sitename={name} — No Auth Required

The only endpoint needed for search engine metadata enrichment. Returns site statistics for any public site.

```json
{
  "result": "success",
  "info": {
    "sitename": "example",
    "hits": 5072,
    "created_at": "Sat, 29 Jun 2013 10:11:38 +0000",
    "last_updated": "Tue, 23 Jul 2013 20:04:03 +0000",
    "domain": null,
    "tags": ["art", "personal"]
  }
}
```

| Field | Type | Search Engine Use |
|-------|------|-------------------|
| `hits` | integer | Popularity ranking signal |
| `last_updated` | RFC2822 | Freshness ranking signal; crawl priority |
| `tags` | string[] | Categorization; potential faceted search |
| `domain` | string/null | Detect custom domains for canonical URLs |
| `created_at` | RFC2822 | Site age signal |

### POST /api/upload — Auth Required

Used only to deploy the search engine's own frontend to its Neocities site. Not used for crawling.

### API Rules

1. Do not spam the server with tons of API requests
2. Limit recurring site updates to one per minute
3. Do not game site rankings
4. **Do not use the API to data mine / rip all of the sites** — this is why discovery uses the browse page, not the API

---

## Cost Estimates

### Railway (monthly, usage-based)

| Service | Estimated Usage | Cost |
|---------|----------------|------|
| search-api (idle most of the time) | ~0.1 vCPU, 256 MB avg | ~$2-4 |
| crawler-pipeline (1hr/day cron) | ~0.5 vCPU, 512 MB for 1hr | ~$1-2 |
| PostgreSQL (few GB) | ~256 MB, 5 GB storage | ~$2-3 |
| Volume (Tantivy index) | 2-5 GB SSD | ~$0.50-1 |
| **Subtotal** | | **~$5-10** |
| Pro plan included credits | -$20 | Absorbs most/all usage |

### Cloudflare R2 (monthly)

| Resource | Usage | Cost |
|----------|-------|------|
| Storage | ~1-3 GB compressed text | Free tier (10 GB included) |
| Class A ops (writes) | ~50K/month (re-crawl) | Free tier (1M included) |
| Class B ops (reads) | ~100K/month (snippets) | Free tier (10M included) |
| Egress | Any amount | **Free** |
| **Subtotal** | | **$0** |

### Neocities (monthly)

| Plan | Cost |
|------|------|
| Free tier (1 GB storage, 200 GB bandwidth) | **$0** |

### Total Estimated Monthly Cost

**$5-10/month**, potentially fully covered by Railway Pro's included credits depending on query volume. R2 and Neocities both stay within free tiers.

---

## Corpus Estimates

| Metric | Estimate |
|--------|----------|
| Active Neocities sites | ~358,000+ |
| Pages per site (avg, up to 50 crawled) | ~10 |
| Total pages | ~3.5M |
| Raw HTML per page (avg) | ~10 KB (downloaded but discarded after extraction) |
| Extracted text per page (avg, after trafilatura) | ~2-5 KB |
| Total extracted text | ~7-17 GB |
| Tantivy index size | ~1.5-4 GB |
| Compressed parsed text in R2 (zstd + dict) | ~1-3 GB |
| Raw HTML stored | **None** — discarded after text extraction to save storage |

---

## Timeline

| Phase | Duration | Description |
|-------|----------|-------------|
| **Setup** | 1-2 days | Railway project, Postgres schema, R2 bucket, Neocities site |
| **Phase 1: Discovery** | 1-2 days | Browse page scraper → full domain list → API metadata enrichment → deduplicated site catalog |
| **Phase 2: Queue build** | <1 day | Build crawl queue with `has_been_indexed`/`last_indexed`/`last_updated` columns; first run queues all sites |
| **Phase 3: Initial crawl** | 5-7 days | ~358K sites at polite crawl speeds (1-2s delay); extract text, discard HTML, compress and store parsed text to R2 |
| **Phase 4: Indexing** | 1-2 days | Tantivy index build from extracted text |
| **Search API** | 1-2 days | FastAPI endpoints, CORS, query parsing |
| **Frontend** | 1-2 days | Static search interface on Neocities |
| **Polish & deploy** | 2-3 days | Error handling, monitoring, incremental crawl cron |
| **Total to MVP** | **~2-3 weeks** | |

---

## Key Design Decisions & Rationale

**Tantivy over Meilisearch:** Meilisearch would provide a faster path to a working demo (REST API, built-in typo tolerance, instant search), but Tantivy gives full control over scoring, index format, and memory layout. For a corpus this large with custom ranking signals, library-level control wins over convenience.

**Tantivy over SQLite FTS5:** FTS5 could run on Cloudflare D1 for a serverless setup, but its ranking (BM25 without Block-Max WAND) is slower at scale, its compression is worse, and it lacks fuzzy search, phrase queries, and field-level weighting. At 3.5M documents, Tantivy's performance advantage becomes significant.

**PostgreSQL over SQLite for operational data:** The crawler and search API are separate Railway services. SQLite is single-process; Postgres handles concurrent access from multiple services over Railway's internal network without locking issues.

**R2 over Railway Volumes for parsed text storage:** Railway volumes are SSD-backed and billed for provisioned size — ideal for the Tantivy index that needs fast random access. The compressed parsed text archive is write-once-read-rarely data that doesn't need SSD performance. R2 at $0.015/GB with zero egress is the economical choice for long-term storage. Raw HTML is discarded after text extraction to minimize storage costs.

**No proxies:** Neocities doesn't run aggressive anti-bot measures. Polite crawling with honest User-Agent identification and 1-2s delays avoids blocks without the cost, complexity, and ethical concerns of proxy rotation. If rate-limited, the correct response is to slow down, not evade.

**trafilatura with `favor_recall=True`:** Neocities sites are hand-crafted HTML with unconventional structure. Trafilatura's default precision mode may miss content that isn't in standard `<article>` or `<main>` tags. `favor_recall=True` casts a wider net, which is correct for personal websites where the entire page is the content.

**Frontend on Neocities, not Cloudflare Pages:** Philosophically coherent (a Neocities search engine on Neocities), and the free tier is more than sufficient for a static search interface. The only constraint is no server-side execution — all search logic happens via `fetch()` to the Railway API.

---

## Technology Summary

```
Frontend:        Neocities (free static hosting)
                 Vanilla HTML/CSS/JS

Search Engine:   Tantivy (Rust, via tantivy-py bindings)
                 BM25 scoring, Elias-Fano posting lists, FST dictionaries

API Server:      FastAPI (Python, async)
                 Hosted on Railway (persistent service, public domain)

Crawler:         httpx (async HTTP) + BeautifulSoup4 (HTML parsing)
                 Hosted on Railway (cron service, daily schedule)

Extraction:      trafilatura (HTML → clean text + metadata)

Database:        PostgreSQL (Railway managed)
                 Site catalog, documents, crawl queue

Index Storage:   Railway Volume (SSD, persistent)
                 ~1.5-4 GB for Tantivy index

Cold Storage:    Cloudflare R2 (S3-compatible, zero egress)
                 Compressed parsed text only (no raw HTML), zstd with trained dictionary

Compression:     zstd (Zstandard) with 64 KB trained dictionary
                 ~5:1 ratio on extracted text

Deployment:      Railway (GitHub push → auto-deploy)
                 Neocities CLI (frontend push)
```
