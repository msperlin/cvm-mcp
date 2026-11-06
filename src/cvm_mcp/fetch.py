"""Download and caching layer for ``dados.cvm.gov.br``.

The portal is a plain nginx index: no API, no auth, no pagination. What it does
have is stable file names and a directory listing with sizes, which is all this
module needs. Two behaviours matter:

* **Revalidate, do not re-download.** CVM republishes archives in place (the
  current month is rewritten daily), so files are cached on disk and checked
  with ``If-Modified-Since`` instead of being fetched on every call.
* **One writer per file.** Several agents may run the same server at once, so
  downloads are guarded by a cross-process ``filelock``, not just an asyncio
  lock.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import random
import re
import time
import zipfile
from email.utils import formatdate
from pathlib import Path
from typing import Final

import httpx
from filelock import FileLock

BASE_URL: Final[str] = "https://dados.cvm.gov.br/dados/"
RETRY_STATUS: Final[frozenset[int]] = frozenset({429, 500, 502, 503, 504})
LISTING_TTL_SECONDS: Final[int] = 3600

_HREF = re.compile(r'href="([^"?][^"]*?)"')


class CVMError(Exception):
    """Base class for expected CVM failures."""


class PeriodNotFound(CVMError):
    """The requested year/month is not published."""


class CVMUnavailable(CVMError):
    """CVM is unreachable or returned an unexpected response."""


def parse_listing(html: str) -> dict[str, str]:
    """Return ``{name: url}`` for the links on an nginx auto-index page."""
    names: dict[str, str] = {}
    for href in _HREF.findall(html):
        if href.startswith(("../", "/", "http")):
            continue
        names[href.rstrip("/")] = href
    return names


def period_regex(url_template: str) -> re.Pattern[str]:
    """Turn ``.../dfp_cia_aberta_{period}.zip`` into a capturing regex."""
    filename = url_template.rsplit("/", 1)[-1]
    pattern = re.escape(filename).replace(r"\{period\}", r"(?P<period>\d{4,6})")
    return re.compile(rf"^{pattern}$", re.IGNORECASE)


class Client:
    """Downloads and caches CVM archives."""

    def __init__(
        self,
        *,
        cache_dir: str | Path | None = None,
        timeout: float = 300.0,
        max_retries: int = 3,
        refresh_hours: float | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        user_agent: str = "cvm-mcp/0.1.0",
    ) -> None:
        if cache_dir is None:
            cache_dir = os.environ.get(
                "CVMMCP_CACHE_DIR", str(Path.home() / ".cache" / "cvm-mcp")
            )
        self.cache_dir = Path(cache_dir)
        self.download_dir = self.cache_dir / "downloads"
        self.meta_dir = self.cache_dir / "meta"
        for directory in (self.download_dir, self.meta_dir):
            directory.mkdir(parents=True, exist_ok=True)

        self.timeout = timeout
        self.max_retries = max(0, max_retries)

        if refresh_hours is None:
            try:
                refresh_hours = float(os.environ.get("CVMMCP_REFRESH_HOURS", "12"))
            except ValueError:
                refresh_hours = 12.0
        self.refresh_seconds = max(0.0, refresh_hours * 3600.0)

        self._listings: dict[str, tuple[float, dict[str, str]]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._transport = transport
        self._user_agent = user_agent
        self._http: httpx.AsyncClient | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def _ensure_http(self) -> httpx.AsyncClient:
        """Return an HTTP client bound to the *current* event loop.

        A server normally lives in one loop, but tests (and embeddings) create a
        fresh loop per call. Reusing a pool across loops fails deep inside anyio,
        so the client is rebuilt when the loop changes.
        """
        loop = asyncio.get_running_loop()
        if self._http is None or self._http.is_closed or self._loop is not loop:
            self._http = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout, connect=30.0),
                transport=self._transport,
                follow_redirects=True,
                headers={"User-Agent": self._user_agent},
            )
            self._loop = loop
        return self._http

    async def aclose(self) -> None:
        if self._http is not None and not self._http.is_closed:
            await self._http.aclose()
        self._http = None
        self._loop = None

    # -- listings -----------------------------------------------------

    async def list_files(self, directory: str, *, refresh: bool = False) -> dict[str, str]:
        """List one CVM directory. ``directory`` is absolute or relative to /dados/."""
        url = directory if directory.startswith("http") else BASE_URL + directory
        if not url.endswith("/"):
            url += "/"

        now = time.time()
        cached = self._listings.get(url)
        if cached is not None and not refresh and cached[0] > now:
            return cached[1]

        response = await self._request("GET", url)
        files = parse_listing(response.text)
        if not files:
            raise CVMUnavailable(f"CVM returned an empty listing for {url}")
        self._listings[url] = (now + LISTING_TTL_SECONDS, files)
        return files

    async def periods(self, dataset, *, refresh: bool = False) -> list[str]:
        """Available ``YYYY`` (yearly) or ``YYYYMM`` (monthly) values."""
        if dataset.period == "none":
            return [""]

        files = await self.list_files(dataset.directory, refresh=refresh)
        regex = period_regex(dataset.url.rsplit("/", 1)[-1])
        periods: set[str] = set()
        for name in files:
            match = regex.match(name)
            if match:
                periods.add(match.group("period"))
        if not periods:
            raise CVMUnavailable(f"no archives found for dataset {dataset.id!r}")
        return sorted(periods)

    # -- downloads ----------------------------------------------------

    async def ensure(self, url: str, *, refresh_hours: float | None = None) -> Path:
        """Download ``url`` if needed and return the local path."""
        name = url.rstrip("/").rsplit("/", 1)[-1]
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
        destination = self.download_dir / f"{digest}-{name}"
        refresh = self.refresh_seconds if refresh_hours is None else refresh_hours * 3600.0

        usable = destination.exists() and self._is_usable(destination)
        if usable and not self._is_stale(destination, refresh):
            return destination

        lock = FileLock(str(destination) + ".lock")
        await asyncio.to_thread(lock.acquire, timeout=600)
        try:
            usable = destination.exists() and self._is_usable(destination)
            if usable and not self._is_stale(destination, refresh):
                return destination

            headers: dict[str, str] = {}
            if usable:
                headers["If-Modified-Since"] = formatdate(
                    destination.stat().st_mtime, usegmt=True
                )
            downloaded = await self._download(url, destination, headers)
            if not downloaded and not usable:
                raise CVMUnavailable(f"CVM kept the file unchanged but {destination} is missing")
            if not self._is_usable(destination):
                destination.unlink(missing_ok=True)
                raise CVMUnavailable(f"downloaded file is not valid: {url}")
            return destination
        finally:
            await asyncio.to_thread(lock.release)

    # -- internals ----------------------------------------------------

    def _lock(self, key: str) -> asyncio.Lock:
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        return self._locks[key]

    @staticmethod
    def _is_stale(path: Path, refresh_seconds: float) -> bool:
        if refresh_seconds <= 0:
            return False
        return (time.time() - path.stat().st_mtime) > refresh_seconds

    @staticmethod
    def _is_usable(path: Path) -> bool:
        if path.suffix.lower() in {".csv", ".txt"}:
            return path.stat().st_size > 1024
        try:
            with zipfile.ZipFile(path) as archive:
                return bool(archive.namelist())
        except (zipfile.BadZipFile, OSError):
            return False

    async def _download(self, url: str, destination: Path, headers: dict[str, str]) -> bool:
        """Stream ``url`` to ``destination``. Returns ``False`` on HTTP 304."""
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + ".part")
        last_error: Exception | None = None

        for attempt in range(self.max_retries + 1):
            http = self._ensure_http()
            try:
                async with http.stream("GET", url, headers=headers) as response:
                    if response.status_code == 304:
                        os.utime(destination, None)
                        return False
                    if response.status_code in RETRY_STATUS:
                        last_error = CVMUnavailable(
                            f"CVM returned HTTP {response.status_code} for {url}"
                        )
                    elif response.status_code >= 400:
                        raise CVMUnavailable(
                            f"CVM returned HTTP {response.status_code} for {url}"
                        )
                    else:
                        with temporary.open("wb") as handle:
                            async for chunk in response.aiter_bytes(1 << 16):
                                handle.write(chunk)
                        os.replace(temporary, destination)
                        return True
            except httpx.HTTPError as exc:
                last_error = exc
            finally:
                temporary.unlink(missing_ok=True)

            if attempt < self.max_retries:
                await asyncio.sleep(min(2**attempt, 8) + random.uniform(0, 0.4))

        raise CVMUnavailable(f"could not download {url}: {last_error}")

    async def _request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            http = self._ensure_http()
            try:
                response = await http.request(method, url, **kwargs)  # type: ignore[arg-type]
            except httpx.HTTPError as exc:
                last_error = exc
            else:
                if response.status_code not in RETRY_STATUS:
                    if response.status_code >= 400:
                        raise CVMUnavailable(
                            f"CVM returned HTTP {response.status_code} for {url}"
                        )
                    return response
                last_error = CVMUnavailable(
                    f"CVM returned HTTP {response.status_code} for {url}"
                )
            if attempt < self.max_retries:
                await asyncio.sleep(min(2**attempt, 8) + random.uniform(0, 0.4))
        raise CVMUnavailable(f"could not reach {url}: {last_error}")


def describe_error(exc: BaseException) -> str:
    """Short, user-facing description of a CVM failure."""
    if isinstance(exc, CVMError):
        return str(exc)
    if isinstance(exc, httpx.TimeoutException):
        return "CVM request timed out"
    if isinstance(exc, httpx.HTTPError):
        return f"CVM connection error: {exc}"
    return f"{type(exc).__name__}: {exc}"
