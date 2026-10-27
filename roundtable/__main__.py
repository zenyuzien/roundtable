"""Start the app:  python -m roundtable [--host 127.0.0.1] [--port 8765]"""
import argparse
import logging

import uvicorn

from .api import create_app


def main() -> None:
    ap = argparse.ArgumentParser(description="Roundtable: synchronous multi-agent research environment")
    ap.add_argument("--host", default="127.0.0.1", help="bind address (default: localhost only)")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx logs full request URLs at INFO; keep it quiet (keys are in headers, but be conservative)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    print(f"Roundtable running at http://{args.host}:{args.port}")
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
