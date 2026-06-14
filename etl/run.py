"""ETL service entrypoint.

Phase 1 scaffold: applies database migrations on boot (idempotent — a no-op
when already at head), then idles. No scheduler is wired yet (CLAUDE.md §2);
real jobs and a scheduler arrive in Phase 2+.
"""

import logging
import subprocess
import time

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("etl")

_IDLE_SECONDS = 3600


def apply_migrations() -> None:
    logger.info("Applying database migrations (alembic upgrade head)...")
    try:
        subprocess.run(["alembic", "-c", "migrations/alembic.ini", "upgrade", "head"], check=True)
    except subprocess.CalledProcessError:
        logger.exception("Database migration failed; aborting ETL startup.")
        raise
    logger.info("Migrations applied.")


def main() -> None:
    apply_migrations()
    logger.info("ETL service idle — no scheduler configured yet.")
    while True:
        time.sleep(_IDLE_SECONDS)


if __name__ == "__main__":
    main()
