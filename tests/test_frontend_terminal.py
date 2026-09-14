"""Browser regression for the shipped terminal layout and stream controls."""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlparse

import pytest


def test_shipped_terminal_bundle_keeps_valid_dimensions_and_detach_state() -> None:
    playwright_api = pytest.importorskip("playwright.sync_api")
    from playwright.sync_api import Error as PlaywrightError

    root = Path(__file__).resolve().parents[1]
    dist = root / "ctfws" / "frontend" / "dist"
    rows = [
        {
            "id": 1,
            "name": "Probe1",
            "context_label": "REVIEW / in-memory transport",
            "status": "active",
            "runtime_available": True,
            "sharing": "private",
        }
    ]
    resize_frames: list[dict[str, object]] = []

    def serve(route) -> None:
        path = urlparse(route.request.url).path
        data = {
            "/api/v1/summary": {"hosts": 0, "active_terminals": 1},
            "/api/v1/connections": [],
            "/api/v1/terminals": rows,
            "/api/v1/paths": [],
            "/api/v1/hosts": [],
            "/api/v1/topology/graph": {"nodes": [], "edges": []},
            "/api/v2/auth/session": {"authenticated": True, "role": "admin"},
            "/api/v2/workspaces": [{"id": 1}],
        }
        if path in data:
            route.fulfill(json=data[path])
        elif path.endswith("/events"):
            route.fulfill(status=200, content_type="text/event-stream", body=": test\n\n")
        elif path.startswith("/api/"):
            route.fulfill(json=[])
        else:
            asset = dist / ("index.html" if path == "/" else path.lstrip("/"))
            if not asset.is_relative_to(dist) or not asset.is_file():
                route.fulfill(status=404)
                return
            mime = {".html": "text/html", ".js": "text/javascript", ".css": "text/css"}
            route.fulfill(
                path=asset, content_type=mime.get(asset.suffix, "application/octet-stream")
            )

    def socket(ws) -> None:
        ws.send(json.dumps({"type": "ready", "control": True, "readonly": False}))
        ws.send(json.dumps({"type": "output", "sequence": 1, "gap": False}))
        ws.send(b"CONDUIT_TEST\r\n")

        def received(message) -> None:
            if isinstance(message, str) and message.startswith("{"):
                payload = json.loads(message)
                if payload.get("action") == "resize":
                    resize_frames.append(payload)

        ws.on_message(received)

    with playwright_api.sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(channel="msedge", headless=True)
        except PlaywrightError as error:
            pytest.skip(f"Microsoft Edge não está disponível para o smoke test: {error}")
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.route("http://conduit-review.test/**", serve)
        page.route_web_socket("**/stream*", socket)
        page.goto("http://conduit-review.test/")
        page.get_by_role("button", name=re.compile("^.*Terminais$")).click()
        page.get_by_role("button", name=re.compile("^.*Probe1")).click()
        page.wait_for_timeout(2500)
        dimensions = page.locator(".xterm-host").evaluate(
            "e => ({height:e.getBoundingClientRect().height, "
            "pageHeight:document.body.scrollHeight})"
        )
        assert resize_frames
        assert all(
            1 <= int(frame["columns"]) <= 500 and 1 <= int(frame["rows"]) <= 500
            for frame in resize_frames
        )
        assert dimensions["height"] < 1000
        page.get_by_role("button", name="Desconectar visualização", exact=True).click()
        assert page.get_by_text("Visualização desconectada", exact=True).count() == 1
        browser.close()
