# IndySearch Deployment Guide

## Architecture Overview

IndySearch has four components, all deployed from **one single Git repository**:

```
┌─────────────────────────────────────────────────────────┐
│                    ONE GitHub Repo                       │
│                                                         │
│  ┌──────────┐  ┌──────────┐  ┌─────────┐  ┌─────────┐ │
│  │ frontend/ │  │ search_  │  │ crawler/│  │pipeline │ │
│  │          │  │  api/     │  │         │  │  .py    │ │
│  └────┬─────┘  └────┬─────┘  └────┬────┘  └────┬────┘ │
│       │              │             │             │       │
└───────┼──────────────┼─────────────┼─────────────┼──────┘
        │              │             │             │
        ▼              ▼             └──────┬──────┘
   Neocities      Railway                  ▼
  (GitHub Actions  API Service         Railway
   auto-deploy)   (Dockerfile.api)     Crawler Service
                       │              (Dockerfile.crawler)
                       │                    │
                       ▼                    ▼
                  ┌─────────┐        ┌───────────┐
                  │ Railway │        │Cloudflare  │
                  │PostgreSQL│       │    R2      │
                  └─────────┘        └───────────┘
```

**You do NOT need separate repos.** Railway lets you deploy multiple services from the same repo — each service just uses a different Dockerfile. The frontend deploys to Neocities via a GitHub Action in the same repo.

| Component | Where it runs | How it deploys | Dockerfile |
|-----------|--------------|----------------|------------|
| **Search API** | Railway (always on) | Auto-deploys on `git push` | `Dockerfile.api` |
| **Crawler** | Railway (monthly cron) | Auto-deploys on `git push` | `Dockerfile.crawler` |
| **PostgreSQL** | Railway (managed) | Created via Railway dashboard | N/A (Railway manages it) |
| **Frontend** | Neocities | GitHub Actions on push to `frontend/` | N/A (static files) |
| **Text Storage** | Cloudflare R2 | N/A (object storage bucket) | N/A |

---

## Prerequisites

Before starting, you need accounts on:

1. **GitHub** — to host the repo (free)
2. **Railway** — to run the API, crawler, and database ($5 Pro plan, includes $5 credit)
3. **Cloudflare** — for R2 object storage (free tier covers this project)
4. **Neocities** — to host the frontend (free tier)

---

## Step 1: Cloudflare R2 Setup

R2 stores the compressed extracted text from crawled pages. The crawler writes to it, and the indexer reads from it during index builds. R2 is S3-compatible with zero egress fees.

> **Can you skip R2?** Yes — if `R2_BUCKET_NAME` is not set, the crawler falls back to local file storage (`STORAGE_DIR`). But this means text files live on the Railway container's ephemeral filesystem and will be lost on redeploy. R2 is strongly recommended for production.

### Create the bucket

1. Log into the [Cloudflare dashboard](https://dash.cloudflare.com)
2. In the left sidebar, click **R2 Object Storage**
3. Click **Create bucket**
4. Name it `indysearch-texts` (or whatever you prefer — just match it in your env vars later)
5. Choose **Automatic** for the location hint
6. Click **Create bucket**

### Create an API token

1. Still in R2, click **Manage R2 API Tokens** (top right)
2. Click **Create API Token**
3. Give it a name like `indysearch-crawler`
4. Permissions: **Object Read & Write**
5. Scope: apply to the `indysearch-texts` bucket only (or all buckets)
6. Click **Create API Token**
7. **Save these three values** — you will need them as environment variables:

| What Cloudflare shows | Env variable it maps to |
|----------------------|------------------------|
| Account ID (shown at top of R2 page) | `R2_ACCOUNT_ID` |
| Access Key ID | `R2_ACCESS_KEY_ID` |
| Secret Access Key | `R2_SECRET_ACCESS_KEY` |

The `R2_ACCOUNT_ID` is also visible in your Cloudflare dashboard URL: `https://dash.cloudflare.com/<THIS-IS-YOUR-ACCOUNT-ID>/r2`

---

## Step 2: Railway Setup

Railway runs three things: the API server, the crawler pipeline, and a PostgreSQL database. All three live inside one Railway "project."

### 2a. Create the Railway project and database

1. Go to [railway.app](https://railway.app) and sign in (GitHub OAuth recommended)
2. Click **New Project** → **Empty Project**
3. Inside the project, click **New** → **Database** → **PostgreSQL**
4. Railway will spin up a Postgres instance. Click on it, then go to the **Variables** tab
5. Find `DATABASE_URL` — it looks like:
   ```
   postgresql://postgres:RANDOM_PASSWORD@HOST.railway.internal:5432/railway
   ```
6. You don't need to copy this manually — Railway lets you reference it as `${{Postgres.DATABASE_URL}}` in other services (see below)

### 2b. Deploy the Search API service

This is the FastAPI server that handles `/search` requests from the frontend. It runs continuously.

1. In your Railway project, click **New** → **GitHub Repo** → select your NeoSearch repo
2. Railway will detect the repo. Now configure it:

**Build settings** (Settings → Build):
- **Root Directory**: `/` (leave as project root)
- **Builder**: `Dockerfile`
- **Dockerfile Path**: `Dockerfile.api`

**Environment variables** (Variables tab → click **New Variable** for each):

| Variable | Value | Notes |
|----------|-------|-------|
| `DATABASE_URL` | `${{Postgres.DATABASE_URL}}` | Click **Add Reference** → select the PostgreSQL service. Railway auto-fills the connection string. |
| `INDEX_DIR` | `/app/data/index` | Path inside the container where the Tantivy index lives. Must match the volume mount path (see below). |
| `PORT` | `8000` | Railway routes traffic to this port. Must match the `uvicorn` command in `Dockerfile.api`. |

**Volume** (Settings → Volumes → Mount Volume):
- Click **New Volume**
- **Mount path**: `/app/data/index`
- This gives the Tantivy index persistent storage. Without a volume, the index would be lost on every redeploy.

**Networking** (Settings → Networking):
- Click **Generate Domain** to get a public URL
- You'll get something like: `https://indysearch-api-production.up.railway.app`
- **Save this URL** — you need it for the frontend `config.js`

### 2c. Deploy the Crawler service

This is the pipeline that discovers sites, crawls them, extracts text, and builds the search index. It runs as a scheduled cron job.

1. In the same Railway project, click **New** → **GitHub Repo** → select the **same** NeoSearch repo again
2. Railway will create a second service from the same repo. Configure it:

**Build settings** (Settings → Build):
- **Root Directory**: `/`
- **Builder**: `Dockerfile`
- **Dockerfile Path**: `Dockerfile.crawler`

**Environment variables** (Variables tab):

| Variable | Value | Notes |
|----------|-------|-------|
| `DATABASE_URL` | `${{Postgres.DATABASE_URL}}` | Same reference as the API service. |
| `INDEX_DIR` | `/app/data/index` | Where to write the Tantivy index. Should match the API's volume mount so both services share the same index. |
| `STORAGE_DIR` | `/app/data/texts` | Local fallback for text storage. Only used if `R2_BUCKET_NAME` is not set. |
| `R2_BUCKET_NAME` | `indysearch-texts` | The name of the R2 bucket you created in Step 1. **If this is set, the crawler uses R2 instead of local storage.** |
| `R2_ACCOUNT_ID` | *(from Step 1)* | Your Cloudflare account ID. |
| `R2_ACCESS_KEY_ID` | *(from Step 1)* | The access key from the R2 API token. |
| `R2_SECRET_ACCESS_KEY` | *(from Step 1)* | The secret key from the R2 API token. |

**Volume** (Settings → Volumes → Mount Volume):
- Mount the **same volume** as the API service at `/app/data/index`
- This ensures the crawler writes the Tantivy index to the same persistent disk that the API reads from

**Cron schedule** (Settings → Deploy → Cron Schedule):
- Set to: `0 3 1 * *`
- This means: run at 3:00 AM UTC on the 1st of every month
- You can adjust the frequency — for initial testing, you might want `0 3 * * *` (daily at 3am)

> **Important:** The crawler service should NOT have a public domain. It doesn't serve HTTP traffic — it just runs the pipeline script and exits.

---

## Step 3: Frontend (Neocities) Setup

The frontend is plain HTML/CSS/JS — no build step needed. It gets deployed to Neocities via a GitHub Action.

### 3a. Update the API URL

Before deploying, you must point the frontend at your Railway API. Edit `frontend/config.js`:

```js
// Change this from localhost to your Railway public URL
const API_BASE = 'https://indysearch-api-production.up.railway.app';
```

Replace with the actual domain you generated in Step 2b. **Do not include a trailing slash.**

### 3b. Get your Neocities API token

1. Log into [neocities.org](https://neocities.org)
2. Go to your site's **Settings** page
3. Under **API Key**, click **Generate** (or copy the existing one)
4. Copy the API key

### 3c. Add the GitHub secret

1. Go to your GitHub repo → **Settings** → **Secrets and variables** → **Actions**
2. Click **New repository secret**
3. Name: `NEOCITIES_API_TOKEN`
4. Value: paste the Neocities API key from step 3b
5. Click **Add secret**

### 3d. Deploy

The GitHub Action (`.github/workflows/deploy-neocities.yml`) triggers automatically when:
- You push to `main` and the push includes changes in the `frontend/` directory
- You manually trigger it from the **Actions** tab → **Deploy Frontend to Neocities** → **Run workflow**

After triggering, check the Actions tab to confirm the deploy succeeded.

---

## Step 4: First Run

The crawler needs to run at least once to populate the database and build the search index. Until it runs, the search API will return empty results.

### Option A: Wait for cron

If you set the cron schedule, the crawler will run automatically at the scheduled time. But you probably don't want to wait a month for your first run.

### Option B: Trigger manually on Railway

1. Go to your Railway project → click the **crawler service**
2. Go to **Settings** → **Deploy** → click **Restart** (or **Deploy** to trigger a fresh build)
3. The crawler will start the full pipeline:
   - **Phase 1** — Discovers ~358K Neocities sites by scraping browse pages, then enriches each with API metadata (~2-4 hours)
   - **Phase 2** — Builds a crawl queue, skipping sites that haven't changed since last crawl (~seconds)
   - **Phase 3** — Crawls queued sites, extracts text, stores to R2 (~many hours for full corpus)
   - **Phase 4** — Builds the Tantivy search index from extracted text (~minutes)
4. Watch progress in the Railway logs (click the service → **Logs** tab)

### Option C: Run a small test first

For testing, you can override the Dockerfile command temporarily. In the crawler service settings, set a custom **Start Command**:

```bash
python pipeline.py --max-pages 5 --max-sites 50
```

This limits discovery to 5 browse pages (~500 sites) and only crawls 50 sites. Good for verifying everything works before doing a full run.

### Verify it's working

Once the crawler has finished at least one run:

1. **Check the API**: Visit `https://your-railway-domain.up.railway.app/admin/stats` in your browser — you should see `"index_loaded": true` and a document count
2. **Test a search**: Visit `https://your-railway-domain.up.railway.app/search?q=hello` — you should get search results
3. **Check the frontend**: Visit your Neocities site and try a search

---

## Environment Variables — Complete Reference

### Search API service (`Dockerfile.api`)

| Variable | Required | Default | Example | Description |
|----------|----------|---------|---------|-------------|
| `DATABASE_URL` | Yes | *(none)* | `postgresql://postgres:abc@host:5432/railway` | PostgreSQL connection string. On Railway, use `${{Postgres.DATABASE_URL}}` to reference it automatically. The API uses this to read crawl stats for the `/admin/stats` endpoint. If not set, stats endpoints return empty data but search still works. |
| `INDEX_DIR` | No | `./data/index` | `/app/data/index` | Path to the Tantivy index directory. On Railway, this should point to a mounted persistent volume so the index survives redeploys. |
| `PORT` | Yes (Railway) | `8000` | `8000` | Port that uvicorn listens on. Railway routes external traffic to this port. |

### Crawler service (`Dockerfile.crawler`)

| Variable | Required | Default | Example | Description |
|----------|----------|---------|---------|-------------|
| `DATABASE_URL` | **Yes** | *(none)* | `postgresql://postgres:abc@host:5432/railway` | PostgreSQL connection string. The crawler reads/writes sites, documents, and queue state. **The pipeline will crash without this.** |
| `INDEX_DIR` | No | `./data/index` | `/app/data/index` | Where to write the Tantivy index. Should be the same persistent volume the API reads from. |
| `STORAGE_DIR` | No | `./data/texts` | `/app/data/texts` | Local directory for storing compressed text files. Only used when R2 is not configured. |
| `R2_BUCKET_NAME` | No* | *(none)* | `indysearch-texts` | Cloudflare R2 bucket name. **If set, the crawler stores text in R2 instead of local disk.** If not set, falls back to `STORAGE_DIR`. *Recommended for production.* |
| `R2_ACCOUNT_ID` | If using R2 | *(none)* | `a1b2c3d4e5f6...` | Your Cloudflare account ID. Found in the Cloudflare dashboard URL or the R2 overview page. |
| `R2_ACCESS_KEY_ID` | If using R2 | *(none)* | `abc123...` | Access key from the R2 API token you created. |
| `R2_SECRET_ACCESS_KEY` | If using R2 | *(none)* | `xyz789...` | Secret key from the R2 API token. **Keep this secret.** |

### GitHub Actions (frontend deploy)

| Variable | Required | Where to set | Description |
|----------|----------|-------------|-------------|
| `NEOCITIES_API_TOKEN` | Yes | GitHub repo → Settings → Secrets → Actions | API key from your Neocities account. Used by the `deploy-to-neocities` GitHub Action to upload `frontend/` files. |

### Frontend (`frontend/config.js`)

This is not an env variable — it's a hardcoded value in a JS file that you edit before deploying:

```js
// Development:
const API_BASE = 'http://localhost:8000';

// Production (change this to your Railway URL):
const API_BASE = 'https://indysearch-api-production.up.railway.app';
```

### How the R2 fallback works

The storage decision logic in `crawler/storage.py` is:

```
Is R2_BUCKET_NAME set?
  ├── Yes → Use R2 (reads R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY)
  └── No  → Use local filesystem (reads STORAGE_DIR, defaults to ./data/texts)
```

For local development, you don't need R2 at all. Just leave `R2_BUCKET_NAME` unset and text files go to `./data/texts/`.

---

## Git & Deployment Flow

### One repo, multiple Railway services

This is the key thing to understand: **Railway deploys services, not repos.** You connect one GitHub repo and create multiple services that each use a different Dockerfile from that repo.

```
GitHub Repo (NeoSearch)
    │
    ├── Railway Service 1: "search-api"
    │     Uses: Dockerfile.api
    │     Always running, serves HTTP
    │
    ├── Railway Service 2: "crawler"
    │     Uses: Dockerfile.crawler
    │     Runs on cron schedule, then exits
    │
    └── Railway Service 3: "PostgreSQL"
          Managed by Railway, no Dockerfile
```

When you `git push` to your repo, **both** Railway services redeploy automatically (since they're connected to the same repo). The API picks up code changes and restarts; the crawler picks up changes but won't actually run until its next cron trigger.

### What each Dockerfile includes

Both Dockerfiles install **all** Python dependencies (crawler + search_api) because:
- The crawler needs `search_api.indexer` for Phase 4 (building the Tantivy index)
- The API needs crawler packages during admin reindex operations

```
Dockerfile.api:
  - Installs: crawler/requirements.txt + search_api/requirements.txt
  - Copies: crawler/ + search_api/
  - Runs: uvicorn search_api.main:app

Dockerfile.crawler:
  - Installs: crawler/requirements.txt + search_api/requirements.txt
  - Copies: crawler/ + search_api/ + pipeline.py
  - Runs: python pipeline.py
```

### Frontend deployment flow

The frontend deploys separately from the backend via GitHub Actions:

```
git push (changes in frontend/) → GitHub Actions → Neocities API → live on neocities.org
```

The workflow only triggers if the push includes changes inside `frontend/`. You can also trigger it manually from the GitHub Actions tab.

---

## Local Development Setup

For testing locally before deploying:

### 1. Start the local database

```bash
docker compose up -d
```

This starts PostgreSQL on `localhost:5432` with:
- Database: `neosearch`
- User: `neosearch`
- Password: `localdev`

### 2. Set up env files

The `.env.example` files are already configured for local dev. Copy them:

```bash
cp crawler/.env.example crawler/.env
cp search_api/.env.example search_api/.env
```

Both default to:
```
DATABASE_URL=postgresql://neosearch:localdev@localhost:5432/neosearch
```

No R2 variables are set, so text storage falls back to `./data/texts/`.

### 3. Install dependencies

```bash
pip install -r crawler/requirements.txt -r search_api/requirements.txt
```

### 4. Run a test crawl

```bash
# Small test: 2 browse pages, 10 sites
python pipeline.py --max-pages 2 --max-sites 10
```

### 5. Start the API

```bash
uvicorn search_api.main:app --reload --port 8000
```

### 6. Test in browser

Open `frontend/index.html` directly in your browser (no server needed — it's static HTML). The default `config.js` already points to `http://localhost:8000`.

---

## Troubleshooting

### "No index found" warning on API startup

The API logs `No index found at /app/data/index` if the Tantivy index doesn't exist yet. This is normal before the first crawler run. Search will return empty results until the crawler completes Phase 4 (indexing).

### Crawler crashes with `KeyError: 'DATABASE_URL'`

`DATABASE_URL` is required for the crawler — it reads from `os.environ["DATABASE_URL"]` directly (not optional). Make sure the variable is set. On Railway, use `${{Postgres.DATABASE_URL}}` as the value so it auto-resolves.

### Crawler crashes with `KeyError: 'R2_ACCOUNT_ID'`

This only happens if `R2_BUCKET_NAME` is set but the other R2 variables are missing. Either set all four R2 variables, or unset `R2_BUCKET_NAME` entirely to use local storage.

### Frontend shows no results / CORS errors

Check two things:
1. `frontend/config.js` points to the correct Railway URL (no trailing slash)
2. Your Neocities domain (`https://indysearch.neocities.org`) is in the CORS allow list in `search_api/main.py`. If you use a different Neocities site name, add it to the `allow_origins` list.

### Railway volume not shared between services

The API and crawler should both mount the same Railway volume at `/app/data/index`. If they have separate volumes, the crawler builds an index that the API can't see. In Railway, you can share a volume across services in the same project.

---

## Cost Estimate

| Service | Monthly cost | Notes |
|---------|-------------|-------|
| Railway API (idle) | ~$2-4 | Minimal CPU/memory when not handling searches |
| Railway Crawler | ~$1-2 | Only runs during cron (hours/month) |
| Railway PostgreSQL | ~$2-3 | Small database |
| Railway Volume | ~$0.50-1 | Tantivy index ~1-2GB |
| Cloudflare R2 | $0 | Free tier: 10GB storage, 10M reads/month |
| Neocities | $0 | Free tier: 1GB storage, 200GB bandwidth |
| **Total** | **~$5-10/month** | Likely covered by Railway Pro plan's $5 credit |
