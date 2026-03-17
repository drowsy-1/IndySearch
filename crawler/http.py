import asyncio
import logging
import signal

import httpx

logger = logging.getLogger(__name__)

USER_AGENT = "IndySearch/1.0 (+https://indysearch.neocities.org/about)"
HTTP_TIMEOUT = 30.0
MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 2.0

shutdown_event = asyncio.Event()


def setup_signal_handlers():
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)


def _handle_signal(sig, _frame):
    logger.info(f"Received {signal.Signals(sig).name}, finishing current operation...")
    shutdown_event.set()


class CrawlerError(Exception):
    pass


async def fetch_with_retry(
    client: httpx.AsyncClient,
    url: str,
    params: dict | None = None,
) -> httpx.Response:
    for attempt in range(MAX_RETRIES + 1):
        try:
            response = await client.get(url, params=params)

            if response.status_code == 429:
                retry_after = int(response.headers.get("Retry-After", 60))
                logger.warning(f"Rate limited (429). Waiting {retry_after}s. Attempt {attempt + 1}/{MAX_RETRIES + 1}")
                await asyncio.sleep(retry_after)
                continue

            response.raise_for_status()
            return response

        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            if attempt == MAX_RETRIES:
                raise CrawlerError(f"Failed to fetch {url} after {MAX_RETRIES + 1} attempts") from exc
            wait = RETRY_BACKOFF_BASE ** attempt
            logger.warning(f"Transient error for {url}: {exc}. Retrying in {wait}s")
            await asyncio.sleep(wait)

        except httpx.HTTPStatusError as exc:
            if exc.response.status_code >= 500 and attempt < MAX_RETRIES:
                wait = RETRY_BACKOFF_BASE ** attempt
                logger.warning(f"Server error {exc.response.status_code} for {url}. Retrying in {wait}s")
                await asyncio.sleep(wait)
            else:
                raise

    raise CrawlerError(f"Failed to fetch {url} after {MAX_RETRIES + 1} attempts")


def make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT},
        timeout=httpx.Timeout(HTTP_TIMEOUT),
        follow_redirects=True,
    )
