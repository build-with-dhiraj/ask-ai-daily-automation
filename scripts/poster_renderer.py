"""Poster renderer: HTML+Jinja to PNG via Playwright (sync API).

Renders the Rubric Scoreboard and Daily Digest posters defined in
`templates/poster_*.html.j2` to retina-density PNG bytes suitable for
posting to Slack via an `image_url` block.

Public API:
    render_poster(surface, snapshot, output_path=None) -> bytes

The caller (Slack payload assembler, C1.3, future) is expected to wrap
this in try/except PosterRenderError and fall back to text-only Block Kit
on failure. See `scripts/README_poster_renderer.md`.

CLI for local iteration:
    python -m scripts.poster_renderer <surface> <sample_json_path>
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Literal

from jinja2 import Environment, FileSystemLoader, select_autoescape

# ── Constants ──────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = REPO_ROOT / "templates"
STATIC_DIR = REPO_ROOT / "static"
FONTS_DIR = STATIC_DIR / "fonts"

VIEWPORT_WIDTH = 640
VIEWPORT_INITIAL_HEIGHT = 100
DEVICE_SCALE_FACTOR = 2
RENDER_TIMEOUT_MS = 30_000  # 30s hard timeout

# Avoid OS-level font hinting drift between macOS dev and Linux CI runners.
CHROMIUM_LAUNCH_ARGS = ["--font-render-hinting=none"]

TEMPLATE_FOR_SURFACE: dict[str, str] = {
    "scoreboard": "poster_scoreboard.html.j2",
    "digest": "poster_digest.html.j2",
}

Surface = Literal["scoreboard", "digest"]


# ── Errors ─────────────────────────────────────────────────────────────
class PosterRenderError(RuntimeError):
    """Raised when a poster render fails. Carries template name + short reason.

    Caller contract (C1.3): catch this and fall back to text-only Block Kit.
    Do NOT crash the pipeline on render failure.
    """

    def __init__(self, template: str, reason: str) -> None:
        self.template = template
        self.reason = reason
        super().__init__(f"PosterRenderError[{template}]: {reason}")


# ── Jinja env (module-level, single instance) ──────────────────────────
_jinja_env = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=select_autoescape(["html", "j2"]),
)


# ── Public API ─────────────────────────────────────────────────────────
def render_poster(
    surface: Surface,
    snapshot: dict[str, Any],
    output_path: Path | None = None,
) -> bytes:
    """Render a poster PNG.

    Args:
        surface: "scoreboard" or "digest"; selects the Jinja template.
        snapshot: dict matching the schema in templates/README.md.
        output_path: optional path to also write the PNG to disk.

    Returns:
        PNG bytes (starts with the 8-byte PNG magic header).

    Raises:
        PosterRenderError: on any rendering, font-loading, or screenshot failure.
    """
    if surface not in TEMPLATE_FOR_SURFACE:
        raise PosterRenderError(
            template=str(surface),
            reason=f"unknown surface (expected scoreboard|digest, got {surface!r})",
        )

    # F9: digest_eyebrow_right is the count chip in the masthead. The builder
    # no longer seeds it; the caller MUST set it after generating the
    # InsightPayload so the rendered chip matches the actual card count.
    # A missing eyebrow at render time is a wiring bug, not a data shape we
    # want to silently paper over with a default.
    if surface == "digest" and not snapshot.get("digest_eyebrow_right"):
        raise PosterRenderError(
            template=TEMPLATE_FOR_SURFACE[surface],
            reason=(
                "digest_eyebrow_right unset at render time; caller must set "
                "it post-LLM (F9 contract)"
            ),
        )

    template_name = TEMPLATE_FOR_SURFACE[surface]

    # Render Jinja to HTML; cheap, fail-fast before launching Chromium.
    # `fonts_base` lets the template emit absolute file:// URLs to
    # self-hosted WOFF2 files in static/fonts/, so the render never reaches
    # the network for typography.
    try:
        html = _jinja_env.get_template(template_name).render(
            fonts_base=FONTS_DIR.as_uri(),
            **snapshot,
        )
    except Exception as e:  # noqa: BLE001 (Jinja errors are varied)
        raise PosterRenderError(template_name, f"jinja render failed: {e}") from e

    png_bytes = _screenshot_html(html, template_name)

    if output_path is not None:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(png_bytes)

    return png_bytes


# ── Chromium screenshot ────────────────────────────────────────────────
def _screenshot_html(html: str, template_name: str) -> bytes:
    """Drive headless Chromium via Playwright sync API and screenshot."""
    # Import inside the function so that callers who never render don't pay
    # the Playwright import cost, and so an uninstalled Playwright is reported
    # as a clean PosterRenderError instead of a module-load crash.
    try:
        from playwright.sync_api import (  # type: ignore[import-not-found]
            Error as PlaywrightError,
            TimeoutError as PlaywrightTimeout,
            sync_playwright,
        )
    except ImportError as e:
        raise PosterRenderError(
            template_name,
            "playwright not installed; run `pip install -r requirements.txt && "
            "python -m playwright install chromium`",
        ) from e

    start = time.monotonic()
    # Write HTML to a tempfile so the page is loaded via file:// URL.
    # Why: the templates reference self-hosted fonts via absolute file://
    # URLs (see static/fonts/). Pages loaded via Playwright's set_content
    # use `about:blank` as the base, and Chromium refuses to resolve
    # file:// URLs from `about:blank` for security reasons. A real file://
    # page allows file:// font fetches. The tempfile + delete-on-exit
    # pattern keeps the runner FS clean.
    tmp_dir = Path(tempfile.mkdtemp(prefix="poster_render_"))
    html_path = tmp_dir / "poster.html"
    try:
        html_path.write_text(html, encoding="utf-8")
        page_url = html_path.as_uri()
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(args=CHROMIUM_LAUNCH_ARGS, headless=True)
                try:
                    context = browser.new_context(
                        viewport={
                            "width": VIEWPORT_WIDTH,
                            "height": VIEWPORT_INITIAL_HEIGHT,
                        },
                        device_scale_factor=DEVICE_SCALE_FACTOR,
                    )
                    page = context.new_page()
                    page.set_default_timeout(RENDER_TIMEOUT_MS)

                    # Load via file:// so font-face url(file://...) can resolve.
                    page.goto(page_url, wait_until="networkidle")

                    # Belt-and-braces: explicitly await font readiness so the
                    # screenshot doesn't catch a fallback-font flash.
                    page.evaluate("document.fonts.ready")

                    png = page.screenshot(
                        full_page=True,
                        type="png",
                        omit_background=False,
                    )
                finally:
                    browser.close()
        except PlaywrightTimeout as e:
            raise PosterRenderError(
                template_name, f"playwright timeout after {RENDER_TIMEOUT_MS}ms: {e}"
            ) from e
        except PlaywrightError as e:
            raise PosterRenderError(template_name, f"playwright error: {e}") from e
        except PosterRenderError:
            raise
        except Exception as e:  # noqa: BLE001 (safety net on the 30s budget)
            raise PosterRenderError(template_name, f"unexpected render failure: {e}") from e
    finally:
        # Clean up tempdir best-effort; a leaked one won't fail the run.
        try:
            html_path.unlink(missing_ok=True)
            tmp_dir.rmdir()
        except OSError:
            pass

    elapsed_ms = int((time.monotonic() - start) * 1000)
    if elapsed_ms > RENDER_TIMEOUT_MS:
        # Soft warning; Playwright should have raised already, but in case.
        raise PosterRenderError(
            template_name, f"render exceeded {RENDER_TIMEOUT_MS}ms budget ({elapsed_ms}ms)"
        )

    if not png.startswith(b"\x89PNG\r\n\x1a\n"):
        raise PosterRenderError(template_name, "output is not a valid PNG")

    return png


# ── CLI ────────────────────────────────────────────────────────────────
def _cli(argv: list[str]) -> int:
    if len(argv) != 2:
        print(
            "usage: python -m scripts.poster_renderer <surface> <sample_json_path>",
            file=sys.stderr,
        )
        print("  surface: scoreboard | digest", file=sys.stderr)
        return 2

    surface_arg, json_path_arg = argv
    if surface_arg not in TEMPLATE_FOR_SURFACE:
        print(f"error: surface must be one of {list(TEMPLATE_FOR_SURFACE)}", file=sys.stderr)
        return 2

    json_path = Path(json_path_arg)
    if not json_path.exists():
        print(f"error: sample JSON not found: {json_path}", file=sys.stderr)
        return 2

    snapshot = json.loads(json_path.read_text())
    ts = int(time.time())
    out = Path(f"/tmp/poster_{surface_arg}_{ts}.png")

    try:
        png = render_poster(surface_arg, snapshot, output_path=out)  # type: ignore[arg-type]
    except PosterRenderError as e:
        print(f"render failed: {e}", file=sys.stderr)
        return 1

    print(f"{out}  ({len(png) / 1024:.1f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
