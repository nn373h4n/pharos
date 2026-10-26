"""Process entry point for the Celery CLI, which needs a module-level application object.

    celery --app pharos.workers.entrypoint worker
    celery --app pharos.workers.entrypoint beat

Only these commands import this module; tests and the API use `create_celery` directly, so they
never read the environment as a side effect of an import.
"""

from pharos.workers.app import create_celery

app = create_celery()
