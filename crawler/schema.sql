CREATE TABLE IF NOT EXISTS sites (
    id                SERIAL PRIMARY KEY,
    sitename          TEXT UNIQUE NOT NULL,
    url               TEXT NOT NULL,
    custom_domain     TEXT,
    hits              INTEGER DEFAULT 0,
    tags              TEXT[] DEFAULT '{}',
    created_at        TIMESTAMPTZ,
    last_updated      TIMESTAMPTZ,
    discovered_at     TIMESTAMPTZ DEFAULT NOW(),
    has_been_indexed  BOOLEAN DEFAULT FALSE,
    last_indexed      TIMESTAMPTZ,
    crawl_allowed     BOOLEAN DEFAULT TRUE,
    page_count        INTEGER DEFAULT 0,
    status            TEXT DEFAULT 'discovered'
);

CREATE INDEX IF NOT EXISTS idx_sites_status ON sites(status);
CREATE INDEX IF NOT EXISTS idx_sites_last_updated ON sites(last_updated DESC);
CREATE INDEX IF NOT EXISTS idx_sites_has_been_indexed ON sites(has_been_indexed);

-- One row per crawled page. Body text is NOT stored here — it lives
-- compressed in storage (local filesystem or S3 bucket) referenced by storage_key.
-- body_snippet holds the first ~500 chars for quick display.
CREATE TABLE IF NOT EXISTS documents (
    id              SERIAL PRIMARY KEY,
    site_id         INTEGER REFERENCES sites(id),
    url             TEXT NOT NULL,
    path            TEXT NOT NULL,
    title           TEXT,
    body_snippet    TEXT,
    description     TEXT,
    author          TEXT,
    word_count      INTEGER DEFAULT 0,
    extracted_at    TIMESTAMPTZ DEFAULT NOW(),
    indexed         BOOLEAN DEFAULT FALSE,
    storage_key          TEXT,
    UNIQUE(site_id, path)
);

CREATE INDEX IF NOT EXISTS idx_documents_indexed ON documents(indexed) WHERE NOT indexed;
CREATE INDEX IF NOT EXISTS idx_documents_site_id ON documents(site_id);

-- Per-site crawl queue. Built by queue.py (Phase 2), consumed by crawl.py (Phase 3).
-- last_crawled vs last_updated drives skip logic: if the site hasn't changed
-- since we last crawled it, skip entirely to save compute.
CREATE TABLE IF NOT EXISTS crawl_queue (
    id               SERIAL PRIMARY KEY,
    site_id          INTEGER REFERENCES sites(id) UNIQUE,
    url              TEXT NOT NULL,
    has_been_indexed BOOLEAN DEFAULT FALSE,
    last_indexed     TIMESTAMPTZ,
    last_crawled     TIMESTAMPTZ,
    last_updated     TIMESTAMPTZ,
    priority         INTEGER DEFAULT 0,
    attempts         INTEGER DEFAULT 0,
    last_attempt     TIMESTAMPTZ,
    status           TEXT DEFAULT 'pending'
);

CREATE INDEX IF NOT EXISTS idx_crawl_queue_pending ON crawl_queue(priority DESC) WHERE status = 'pending';

-- Snapshot of stats after each completed crawl session.
CREATE TABLE IF NOT EXISTS crawl_stats (
    id              SERIAL PRIMARY KEY,
    completed_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    sites_crawled   INTEGER NOT NULL DEFAULT 0,
    pages_indexed   INTEGER NOT NULL DEFAULT 0,
    total_sites     INTEGER NOT NULL DEFAULT 0,
    total_documents INTEGER NOT NULL DEFAULT 0
);
