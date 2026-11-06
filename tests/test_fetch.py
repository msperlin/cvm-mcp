"""Download cache: revalidation must not fake a new archive version."""

from __future__ import annotations

import asyncio
import json
import time

import httpx

from cvm_mcp.fetch import Client


def make_client(tmp_path, handler) -> Client:
    return Client(
        cache_dir=tmp_path,
        transport=httpx.MockTransport(handler),
        refresh_hours=1e-9,
    )


def test_304_keeps_the_archive_mtime_and_updates_checked_at(tmp_path):
    calls: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(dict(request.headers))
        if len(calls) == 1:
            return httpx.Response(
                200,
                content=b"x" * 2048,
                headers={
                    "Last-Modified": "Wed, 01 Jan 2025 00:00:00 GMT",
                    "ETag": '"v1"',
                },
            )
        return httpx.Response(304)

    client = make_client(tmp_path, handler)
    url = "https://example.test/data.csv"

    first = asyncio.run(client.ensure(url))
    first_mtime = first.stat().st_mtime
    meta_path = first.with_name(first.name + ".meta.json")
    first_meta = json.loads(meta_path.read_text(encoding="utf-8"))
    time.sleep(0.02)

    second = asyncio.run(client.ensure(url))

    assert second == first
    # The source did not change, so neither should the version mtime. Otherwise
    # Store.ensure() would rebuild the Parquet partition for nothing.
    assert first.stat().st_mtime == first_mtime
    assert "if-modified-since" in calls[1]

    second_meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert second_meta["checked_at"] > first_meta["checked_at"]
    assert second_meta["last_modified"] == "Wed, 01 Jan 2025 00:00:00 GMT"
    assert second_meta["etag"] == '"v1"'
    assert second_meta["url"] == url


def test_a_new_download_advances_the_archive_mtime(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"y" * 2048)

    client = make_client(tmp_path, handler)
    url = "https://example.test/data.csv"

    first = asyncio.run(client.ensure(url))
    first_mtime = first.stat().st_mtime
    time.sleep(0.02)
    second = asyncio.run(client.ensure(url))

    assert second == first
    assert second.stat().st_mtime > first_mtime
