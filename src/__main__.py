"""`python -m src` entrypoint used by the Actor's Docker image."""

import asyncio

# Must run BEFORE `apify` is imported (main.py imports it) so the SDK's run
# validator compiles knowing the 'MCP' run origin. See src/_compat.py.
from ._compat import patch_meta_origin

patch_meta_origin()

from .main import main  # noqa: E402 — intentionally after the compat patch

asyncio.run(main())
