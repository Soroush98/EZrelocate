"""`python -m src` entrypoint used by the Actor's Docker image."""

import asyncio

from .main import main

asyncio.run(main())
