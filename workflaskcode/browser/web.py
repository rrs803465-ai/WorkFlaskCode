from __future__ import annotations

import base64
import ipaddress
import os
import socket
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, urljoin, urlparse

from workflaskcode.errors import WorkFlaskError

INSTALL_HINT = "pip install playwright && python3 -m playwright install chromium"
DEFAULT_TIMEOUT = 25.0
MAX_LINKS = 25
MAX_FETCH_CHARS = 60_000
DESKTOP = (1280, 800)
MOBILE = (390, 844)
SEARCH_ENGINES = {
    "duckduckgo": "https://html.duckduckgo.com/html/?q={q}",
    "bing": "https://www.bing.com/search?q={q}",
}
PROXY_VARS = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy")


class BrowserUnavailable(WorkFlaskError):
    def __init__(self, message: str, *, needs_binaries: bool = False) -> None:
        super().__init__(message)
        self.needs_binaries = needs_binaries


class WebBlocked(WorkFlaskError):
    pass


def playwright_status() -> tuple[bool, str]:
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False, f"Playwright is not installed. Enable it with: {INSTALL_HINT}"
    return True, "playwright package importable (browser binaries are checked when a page is opened)"


def install_chromium(timeout: float = 600.0) -> tuple[bool, str]:
    try:
        done = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    tail = (done.stdout + done.stderr).strip().splitlines()[-3:]
    return done.returncode == 0, " | ".join(tail)


def _bypass_entries(raw: str) -> list[str]:
    out = []
    for entry in (e.strip() for e in raw.split(",")):
        if not entry or entry in ("::", "*"):
            continue
        if ":" in entry and "/" not in entry and not entry.startswith("["):
            entry = f"[{entry}]"
        out.append(entry)
    return out


def _proxy_settings() -> dict[str, str] | None:
    for var in PROXY_VARS:
        value = os.environ.get(var)
        if value:
            parsed = urlparse(value if "://" in value else f"http://{value}")
            if not parsed.hostname:
                continue
            proxy = {
                "server": f"{parsed.scheme}://{parsed.hostname}" + (f":{parsed.port}" if parsed.port else "")
            }
            if parsed.username:
                proxy["username"] = parsed.username
                proxy["password"] = parsed.password or ""
            configured = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
            entries = ["localhost", "127.0.0.1", "[::1]", *_bypass_entries(configured)]
            proxy["bypass"] = ",".join(dict.fromkeys(entries))
            return proxy
    return None


def _addresses(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        return [ipaddress.ip_address(host)]
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return []
    found = []
    for info in infos:
        try:
            found.append(ipaddress.ip_address(info[4][0].split("%")[0]))
        except ValueError:
            continue
    return found


def classify_host(host: str, *, via_proxy: bool | None = None) -> str:
    host = host.strip("[]").lower().rstrip(".")
    if not host:
        raise WebBlocked("URL has no host")
    if host == "localhost" or host.endswith(".localhost"):
        return "loopback"
    addresses = _addresses(host)
    if not addresses:
        if via_proxy if via_proxy is not None else _proxy_settings() is not None:
            return "public"
        raise WebBlocked(f"cannot resolve host '{host}'")
    if all(a.is_loopback for a in addresses):
        return "loopback"
    if any(not a.is_global for a in addresses):
        raise WebBlocked(
            f"'{host}' resolves to a private, link-local or reserved address; the agent only reaches localhost and the public internet"
        )
    return "public"


def check_url(url: str, *, allow_loopback: bool) -> tuple[str, str]:
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https"):
        raise WebBlocked("only http and https URLs are allowed")
    if parsed.username or parsed.password:
        raise WebBlocked("URLs with embedded credentials are not allowed")
    kind = classify_host(parsed.hostname or "")
    if kind == "loopback" and not allow_loopback:
        raise WebBlocked("this tool reaches the public internet only; use browser_check for localhost")
    return parsed.geturl(), kind


@contextmanager
def open_browser(*, allow_loopback: bool, use_proxy: bool | None = None) -> Iterator[object]:
    ok, message = playwright_status()
    if not ok:
        raise BrowserUnavailable(message)
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        proxy = _proxy_settings() if (not allow_loopback if use_proxy is None else use_proxy) else None
        try:
            browser = pw.chromium.launch(**({"proxy": proxy} if proxy else {}))
        except PlaywrightError as exc:
            first = str(exc).splitlines()[0]
            raise BrowserUnavailable(
                f"Chromium could not be launched ({first}). Install it with: python3 -m playwright install chromium",
                needs_binaries="Executable doesn't exist" in str(exc) or "playwright install" in str(exc),
            ) from exc
        try:
            context = browser.new_context(accept_downloads=False, service_workers="block")
            verdicts: dict[str, bool] = {}

            def allowed(host: str) -> bool:
                if host not in verdicts:
                    try:
                        verdicts[host] = (
                            classify_host(host, via_proxy=proxy is not None) != "loopback" or allow_loopback
                        )
                    except WebBlocked:
                        verdicts[host] = False
                return verdicts[host]

            def guard(route) -> None:
                url = route.request.url
                if url.startswith(("data:", "blob:", "about:")):
                    route.continue_()
                    return
                if not allowed(urlparse(url).hostname or ""):
                    route.abort("blockedbyclient")
                    return
                try:
                    response = route.fetch(max_redirects=0)
                except PlaywrightError:
                    route.abort("failed")
                    return
                location = response.headers.get("location")
                if 300 <= response.status < 400 and location:
                    target = urlparse(urljoin(url, location))
                    if target.scheme not in ("http", "https") or not allowed(target.hostname or ""):
                        route.abort("blockedbyclient")
                        return
                route.fulfill(response=response)

            context.route("**/*", guard)
            yield context
        finally:
            browser.close()


def _explain(exc: Exception) -> WorkFlaskError:
    first = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
    if "ERR_CERT" in first or "SSL" in first:
        return WorkFlaskError(
            f"page load failed: {first}. The TLS certificate was not trusted (corporate proxy or bad site); verification is never disabled."
        )
    if "ERR_BLOCKED_BY_CLIENT" in first:
        return WorkFlaskError("the page redirected to a blocked (private or non-local) address")
    return WorkFlaskError(f"page load failed: {first}")


@dataclass
class PageContent:
    url: str
    title: str
    status: int | None
    text: str
    description: str = ""
    links: list[tuple[str, str]] = field(default_factory=list)
    truncated: bool = False


_EXTRACT_JS = """() => {
  const links = [...document.querySelectorAll('a[href]')].slice(0, 400).map(a => [(a.innerText || a.getAttribute('aria-label') || '').trim().slice(0, 90), a.href]);
  const meta = document.querySelector('meta[name=description]');
  return {text: document.body ? document.body.innerText : '', links, description: meta ? meta.content : ''};
}"""


def fetch_page(
    url: str, *, allow_loopback: bool = False, timeout: float = DEFAULT_TIMEOUT, max_chars: int = 8000
) -> PageContent:
    target, _ = check_url(url, allow_loopback=allow_loopback)
    from playwright.sync_api import Error as PlaywrightError

    with open_browser(allow_loopback=allow_loopback) as context:
        page = context.new_page()
        try:
            response = page.goto(target, timeout=timeout * 1000, wait_until="domcontentloaded")
            try:
                page.wait_for_load_state("load", timeout=5000)
            except PlaywrightError:
                pass
            ctype = (response.headers.get("content-type", "") if response else "").lower()
            if ctype and not any(t in ctype for t in ("html", "text", "json", "xml")):
                raise WorkFlaskError(
                    f"not a text page (content-type {ctype.split(';')[0]}); download it yourself if you need it"
                )
            data = page.evaluate(_EXTRACT_JS)
            title = page.title()
            final_url = page.url
        except PlaywrightError as exc:
            raise _explain(exc) from exc
        status = response.status if response else None
    text = (
        " ".join(data["text"].split())
        if len(data["text"]) < 200
        else "\n".join(line.strip() for line in data["text"].splitlines() if line.strip())
    )
    limit = max(500, min(max_chars, MAX_FETCH_CHARS))
    seen: set[str] = set()
    links: list[tuple[str, str]] = []
    for label, href in data["links"]:
        if href.startswith(("http://", "https://")) and href not in seen and label:
            seen.add(href)
            links.append((label, href))
        if len(links) >= MAX_LINKS:
            break
    return PageContent(
        final_url, title, status, text[:limit], data.get("description", ""), links, len(text) > limit
    )


@dataclass
class SearchHit:
    title: str
    url: str
    snippet: str


_DDG_JS = """() => [...document.querySelectorAll('.result')].map(r => {
  const a = r.querySelector('a.result__a'); const s = r.querySelector('.result__snippet');
  return a ? {title: a.innerText.trim(), href: a.href, snippet: s ? s.innerText.trim() : ''} : null;
}).filter(Boolean)"""
_BING_JS = """() => [...document.querySelectorAll('li.b_algo')].map(r => {
  const a = r.querySelector('h2 a'); const s = r.querySelector('.b_caption p, p');
  return a ? {title: a.innerText.trim(), href: a.href, snippet: s ? s.innerText.trim() : ''} : null;
}).filter(Boolean)"""
_EXTRACTORS = {"duckduckgo": _DDG_JS, "bing": _BING_JS}


def unwrap_result_url(href: str) -> str | None:
    parsed = urlparse(href)
    query = parse_qs(parsed.query)
    if "uddg" in query:
        href = query["uddg"][0]
    elif parsed.netloc.endswith("bing.com") and "u" in query and query["u"][0].startswith("a1"):
        raw = query["u"][0][2:]
        try:
            href = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return None
    elif parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/y.js"):
        return None
    final = urlparse(href)
    if final.scheme not in ("http", "https") or not final.hostname:
        return None
    return href


def web_search(
    query: str,
    *,
    max_results: int = 6,
    engines: dict[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    allow_loopback: bool = False,
) -> tuple[list[SearchHit], list[str]]:
    query = " ".join(query.split())
    if not query:
        raise WorkFlaskError("empty search query")
    from playwright.sync_api import Error as PlaywrightError

    notes: list[str] = []
    hits: list[SearchHit] = []
    templates = engines or SEARCH_ENGINES
    with open_browser(allow_loopback=allow_loopback) as context:
        for name, template in templates.items():
            page = context.new_page()
            try:
                url, _ = check_url(template.format(q=quote_plus(query)), allow_loopback=allow_loopback)
                page.goto(url, timeout=timeout * 1000, wait_until="domcontentloaded")
                rows = page.evaluate(_EXTRACTORS.get(name, _DDG_JS))
            except (PlaywrightError, WebBlocked) as exc:
                notes.append(f"{name}: {str(exc).splitlines()[0]}")
                continue
            finally:
                page.close()
            seen: set[str] = set()
            for row in rows:
                target = unwrap_result_url(row["href"])
                key = (urlparse(target).hostname or "") + urlparse(target).path if target else ""
                if not target or key in seen:
                    continue
                seen.add(key)
                hits.append(SearchHit(row["title"][:200], target, row["snippet"][:400]))
                if len(hits) >= max_results:
                    break
            if hits:
                break
            notes.append(f"{name}: no results (the engine may have shown a bot check)")
    return hits, notes


@dataclass
class BrowserReport:
    url: str
    title: str | None = None
    status: int | None = None
    console_errors: list[str] = field(default_factory=list)
    page_errors: list[str] = field(default_factory=list)
    found: dict[str, bool] = field(default_factory=dict)
    screenshots: list[str] = field(default_factory=list)
    failed_requests: list[str] = field(default_factory=list)
    overflow: dict[str, bool] = field(default_factory=dict)
    broken_images: int = 0
    missing_alt: int = 0
    h1_count: int = 0
    has_viewport_meta: bool = True
    lang: str | None = None
    text_excerpt: str = ""
    has_description: bool = True
    unnamed_controls: int = 0
    actions_done: list[str] = field(default_factory=list)
    action_errors: list[str] = field(default_factory=list)

    @property
    def problems(self) -> list[str]:
        out = []
        if (self.status or 0) >= 400 or self.status is None:
            out.append(f"HTTP status {self.status}")
        out += [f"console error: {e}" for e in self.console_errors]
        out += [f"page error: {e}" for e in self.page_errors]
        out += [f"failed request: {e}" for e in self.failed_requests]
        out += [f"selector missing: {s}" for s, ok in self.found.items() if not ok]
        out += [f"horizontal overflow at {name} width" for name, over in self.overflow.items() if over]
        if self.broken_images:
            out.append(f"{self.broken_images} broken image(s)")
        if not self.has_viewport_meta:
            out.append("no <meta name=viewport>: the page will not be responsive on phones")
        if self.h1_count != 1:
            out.append(f"{self.h1_count} <h1> elements (expected 1)")
        if self.missing_alt:
            out.append(f"{self.missing_alt} image(s) without alt text")
        if not self.lang:
            out.append("<html> has no lang attribute")
        if not self.title:
            out.append("page has no <title>")
        if not self.has_description:
            out.append("no <meta name=description>")
        if self.unnamed_controls:
            out.append(f"{self.unnamed_controls} link(s) or button(s) without accessible text")
        out += [f"action failed: {e}" for e in self.action_errors]
        return out

    @property
    def ok(self) -> bool:
        return not self.problems

    def render(self) -> str:
        lines = [f"url: {self.url}", f"http status: {self.status}", f"title: {self.title!r}"]
        for selector, present in self.found.items():
            lines.append(f"selector {selector}: {'found' if present else 'MISSING'}")
        for name, over in self.overflow.items():
            lines.append(f"{name} viewport: {'HORIZONTAL OVERFLOW' if over else 'fits'}")
        lines += [f"action ok: {a}" for a in self.actions_done]
        lines += [f"problem: {p}" for p in self.problems]
        lines += [f"screenshot: {s}" for s in self.screenshots]
        if self.text_excerpt:
            lines.append(f"visible text starts: {self.text_excerpt}")
        lines.append("result: " + ("PASS" if self.ok else "FAIL"))
        return "\n".join(lines)


_METRICS_JS = """() => ({
  overflow: document.documentElement.scrollWidth > window.innerWidth + 1,
  broken: [...document.images].filter(i => i.complete && i.naturalWidth === 0).length,
  noAlt: [...document.images].filter(i => !i.hasAttribute('alt')).length,
  h1: document.querySelectorAll('h1').length,
  viewport: !!document.querySelector('meta[name=viewport]'),
  lang: document.documentElement.getAttribute('lang'),
  desc: !!document.querySelector('meta[name=description][content]:not([content=""])'),
  unnamed: [...document.querySelectorAll('a[href],button')].filter(e => !(e.innerText || '').trim() && !e.getAttribute('aria-label') && !e.getAttribute('title') && !e.querySelector('img[alt]:not([alt=""])')).length,
  text: document.body ? document.body.innerText.trim().slice(0, 400) : ''
})"""


MAX_ACTIONS = 12


def _run_actions(page, actions: list[dict], report: BrowserReport, timeout: float) -> None:
    from playwright.sync_api import Error as PlaywrightError

    for step in actions[:MAX_ACTIONS]:
        kind = str(step.get("do", ""))
        selector = str(step.get("selector", ""))
        label = f"{kind} {selector}".strip()
        try:
            if kind == "click":
                page.click(selector, timeout=timeout * 1000)
            elif kind == "fill":
                page.fill(selector, str(step.get("value", "")), timeout=timeout * 1000)
            elif kind == "press":
                page.press(selector or "body", str(step.get("value", "Enter")), timeout=timeout * 1000)
            elif kind == "wait_for":
                page.wait_for_selector(selector, timeout=timeout * 1000)
            else:
                report.action_errors.append(f"unknown action '{kind}' (use click, fill, press, wait_for)")
                continue
            page.wait_for_load_state("domcontentloaded")
            report.actions_done.append(label)
        except PlaywrightError as exc:
            report.action_errors.append(f"{label}: {str(exc).splitlines()[0]}")


def browser_check(
    url: str,
    *,
    selectors: list[str] | None = None,
    screenshot: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    allow_loopback: bool = True,
    mobile: bool = True,
    actions: list[dict] | None = None,
) -> BrowserReport:
    target, _ = check_url(url, allow_loopback=allow_loopback)
    from playwright.sync_api import Error as PlaywrightError

    report = BrowserReport(target)
    sizes = [("desktop", DESKTOP)] + ([("mobile", MOBILE)] if mobile else [])
    with open_browser(allow_loopback=allow_loopback) as context:
        try:
            for index, (name, (width, height)) in enumerate(sizes):
                page = context.new_page()
                page.set_viewport_size({"width": width, "height": height})
                if index == 0:
                    page.on(
                        "console",
                        lambda m: report.console_errors.append(m.text[:300]) if m.type == "error" else None,
                    )
                    page.on("pageerror", lambda e: report.page_errors.append(str(e)[:300]))
                    page.on(
                        "requestfailed", lambda r: report.failed_requests.append(f"{r.method} {r.url}"[:200])
                    )
                response = page.goto(target, timeout=timeout * 1000, wait_until="load")
                if index == 0 and actions:
                    _run_actions(page, actions, report, timeout)
                metrics = page.evaluate(_METRICS_JS)
                report.overflow[name] = bool(metrics["overflow"])
                if index == 0:
                    report.status = response.status if response else None
                    report.title = page.title()
                    report.broken_images, report.missing_alt = metrics["broken"], metrics["noAlt"]
                    report.h1_count, report.has_viewport_meta = metrics["h1"], bool(metrics["viewport"])
                    report.has_description, report.unnamed_controls = (
                        bool(metrics["desc"]),
                        metrics["unnamed"],
                    )
                    report.lang, report.text_excerpt = (
                        metrics["lang"],
                        " ".join(metrics["text"].split())[:300],
                    )
                    for selector in selectors or []:
                        report.found[selector] = page.query_selector(selector) is not None
                if screenshot is not None:
                    path = (
                        screenshot
                        if index == 0
                        else screenshot.with_name(f"{screenshot.stem}-{name}{screenshot.suffix}")
                    )
                    path.parent.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(path), full_page=True)
                    report.screenshots.append(str(path))
                page.close()
        except PlaywrightError as exc:
            raise _explain(exc) from exc
    return report
