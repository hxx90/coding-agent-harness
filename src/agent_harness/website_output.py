"""Preview and export helpers for generated static websites."""

from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path

from .config import WorkspaceConfig
from .workspace import MAX_FILE_BYTES, iter_visible_files, read_utf8


CSS_LINK_RE = re.compile(
    r"<link\b(?=[^>]*\bhref\s*=\s*['\"]styles\.css['\"])[^>]*>",
    re.IGNORECASE,
)
APP_SCRIPT_RE = re.compile(
    r"<script\b(?=[^>]*\bsrc\s*=\s*['\"]app\.js['\"])[^>]*>\s*</script>",
    re.IGNORECASE,
)
MAX_EXPORT_BYTES = 25 * 1024 * 1024


def website_exists(root: Path) -> bool:
    return all((root / name).is_file() for name in ("index.html", "styles.css", "app.js"))


def build_inline_preview(root: Path) -> str:
    """Inline the required local assets for a sandboxed Streamlit iframe."""
    html = read_utf8(root / "index.html", max_bytes=MAX_FILE_BYTES)
    css = read_utf8(root / "styles.css", max_bytes=MAX_FILE_BYTES)
    javascript = read_utf8(root / "app.js", max_bytes=MAX_FILE_BYTES)
    style = "<style>\n%s\n</style>" % css.replace("</style", "<\\/style")
    script = "<script>\n%s\n</script>" % javascript.replace(
        "</script", "<\\/script"
    )
    if CSS_LINK_RE.search(html):
        html = CSS_LINK_RE.sub(lambda _: style, html, count=1)
    else:
        html = html.replace("</head>", style + "\n</head>", 1)
    if APP_SCRIPT_RE.search(html):
        html = APP_SCRIPT_RE.sub(lambda _: script, html, count=1)
    else:
        html = html.replace("</body>", script + "\n</body>", 1)
    return html


def build_website_zip(config: WorkspaceConfig) -> bytes:
    """Export visible product files while excluding tests and Harness policy files."""
    output = io.BytesIO()
    total = 0
    included = 0
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for relative, path in iter_visible_files(config):
            if not config.can_write(relative):
                continue
            size = path.stat().st_size
            total += size
            if total > MAX_EXPORT_BYTES:
                raise ValueError("网站产物超过 25 MB 导出上限")
            archive.write(path, arcname="website/" + relative)
            included += 1
    if included == 0:
        raise ValueError("当前没有可导出的网站文件")
    return output.getvalue()
