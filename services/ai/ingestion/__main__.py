"""`python -m ai.ingestion` — the entry point the ai-ingestion-worker container runs."""

import argparse
import asyncio

from ai.ingestion.menu_ingestion import main

parser = argparse.ArgumentParser(description="AI Service ingestion worker")
parser.add_argument(
    "--backfill", action="store_true",
    help="ingest every published menu once, then exit (instead of consuming events)",
)
asyncio.run(main(parser.parse_args().backfill))
