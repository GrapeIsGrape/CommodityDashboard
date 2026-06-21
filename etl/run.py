"""ETL service entrypoint.

Applies database migrations on boot first (idempotent — a no-op when already at
head, current behavior preserved), then runs the scheduling layer (#23). The
same image is portable across targets (CLAUDE.md §2):

* ``python -m etl.run`` (the Dockerfile CMD / Compose default) runs the
  long-running in-process scheduler — it ticks once a minute and fires each
  ET-anchored slot at its time.
* ``python -m etl.run --slot NAME`` applies migrations then fires one named slot
  and exits — the one-shot mode an external cron (Railway / Synology DSM) drives.

Slot timing and the timezone are config/env-driven (config/scheduler.yaml +
ETL_TZ); the session-window self-guard keeps timing-sensitive market-data work
correct even under a UTC-only external cron.
"""

import argparse
import logging
import subprocess

from etl import scheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("etl")


def apply_migrations() -> None:
    logger.info("Applying database migrations (alembic upgrade head)...")
    try:
        subprocess.run(["alembic", "-c", "migrations/alembic.ini", "upgrade", "head"], check=True)
    except subprocess.CalledProcessError:
        logger.exception("Database migration failed; aborting ETL startup.")
        raise
    logger.info("Migrations applied.")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="etl.run", description="CommodityDashboard ETL runner.")
    parser.add_argument(
        "--slot",
        metavar="NAME",
        default=None,
        help="Fire a single named slot then exit (one-shot mode for external "
        "crons). Omit to run the long-running in-process scheduler.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    apply_migrations()
    if args.slot:
        logger.info("One-shot mode: firing slot %s then exiting.", args.slot)
        scheduler.run_slot(args.slot)
        return
    logger.info("Starting the in-process ETL scheduler.")
    scheduler.run_forever()


if __name__ == "__main__":
    main()
