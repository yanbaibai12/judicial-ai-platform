from __future__ import annotations

import argparse
import logging

from .config import get_settings
from .logging import setup_logging
from .services.jobs import run_worker_loop

logger = logging.getLogger("judicial_ai.worker")


def main() -> None:
    parser = argparse.ArgumentParser(description="司法 AI 中台异步任务 Worker")
    parser.add_argument("--once", action="store_true", help="处理一轮后退出")
    parser.add_argument("--poll-seconds", type=float, default=None)
    parser.add_argument("--batch-size", type=int, default=None)
    args = parser.parse_args()

    settings = get_settings()
    setup_logging(settings.log_level)
    logger.info(
        "worker_started",
        extra={"status": "RUNNING", "job_type": "worker"},
    )
    run_worker_loop(
        once=args.once,
        poll_seconds=args.poll_seconds,
        batch_size=args.batch_size,
    )
    logger.info("worker_stopped", extra={"status": "STOPPED", "job_type": "worker"})


if __name__ == "__main__":
    main()
