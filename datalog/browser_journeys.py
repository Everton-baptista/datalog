"""Bounded Chromium journeys confined to the operator's configured origin."""

import os
import time
from urllib.parse import urlsplit

from datalog.validation import Invalid, text

ACTIONS = {"navigate", "click", "fill", "assert_text", "assert_visible"}


def steps(body):
    if not isinstance(body, list) or len(body) > 12:
        raise Invalid("Provide up to 12 browser steps")
    result = []
    for step in body:
        if not isinstance(step, dict) or step.get("action") not in ACTIONS:
            raise Invalid("Use navigate, click, fill, assert_text or assert_visible")
        action = step["action"]
        clean = {"action": action, "name": text(step.get("name"), "step", action, 80)}
        if action == "navigate":
            path = text(step.get("path"), "path", limit=500)
            if not path.startswith("/") or path.startswith("//") or "\\" in path or any(c.isspace() for c in path):
                raise Invalid("Navigation must use an absolute path on the configured origin")
            clean["path"] = path
        else:
            clean["selector"] = text(step.get("selector"), "selector", limit=300)
            if action in ("assert_text", "fill"):
                if action == "fill" and "value_env" in step:
                    name = text(step["value_env"], "test variable", limit=100)
                    if name not in {"username", "password"}:
                        raise Invalid("Use username or password as the credential alias")
                    clean["value_env"] = name
                else:
                    clean["value"] = text(step.get("value"), "value", limit=500)
        result.append(clean)
    return result


def run(url, plan, chromium, secrets):
    from playwright.sync_api import Error, expect, sync_playwright

    location = urlsplit(url)
    origin = f"{location.scheme}://{location.netloc}"
    timeline = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=chromium, timeout=8000)
        try:
            context = browser.new_context(service_workers="block", accept_downloads=False)
            context.route(
                "**/*",
                lambda route: route.continue_() if urlsplit(route.request.url)[:2] == location[:2] else route.abort(),
            )
            context.route_web_socket("**/*", lambda websocket: websocket.close())
            page = context.new_page()
            page.set_default_timeout(2000)
            page.set_default_navigation_timeout(4000)
            for step in [{"action": "open", "name": "Open destination"}, *plan]:
                started = time.monotonic()
                action = step["action"]
                try:
                    if action in ("navigate", "open"):
                        response = page.goto(
                            origin + step["path"] if action == "navigate" else url, wait_until="domcontentloaded"
                        )
                        if response is None or not response.ok:
                            raise ValueError("HTTP")
                    else:
                        target = page.locator(step["selector"])
                        if action == "click":
                            target.click()
                        elif action == "fill":
                            value = (
                                os.environ.get(secrets.get(step["value_env"], ""))
                                if "value_env" in step
                                else step["value"]
                            )
                            if value is None:
                                raise ValueError("Missing credential")
                            target.fill(value)
                        elif action == "assert_text":
                            expect(target).to_contain_text(step["value"], timeout=2000)
                        else:
                            expect(target).to_be_visible(timeout=2000)
                except (Error, AssertionError, ValueError):
                    ok = False
                else:
                    ok = urlsplit(page.url)[:2] == location[:2]
                timeline.append(
                    {
                        "name": step["name"],
                        "action": action,
                        "ok": ok,
                        "duration_ms": round((time.monotonic() - started) * 1000, 2),
                    }
                )
                if not ok:
                    break
        finally:
            browser.close()
    return timeline
