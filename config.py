"""Loads .env into the process environment.

Import this before reading any os.getenv() value:

    import config  # noqa: F401 — loads .env before the getenv calls below

The import itself does the work — `load_dotenv()` runs once at module import,
and Python caches modules, so importing it from several places is harmless
rather than repeated work. It lives in its own module so the ordering is
explicit: `database.py` reads DATABASE_URL at *import* time, so anything that
populates the environment has to have run before that line, and burying the
call inside one of the consumers makes every other consumer depend on import
order to work.

Why this is needed at all: docker-compose passes `env_file: .env` and so hands
the container a populated environment already, but a bare `uvicorn main:app`
does not — nothing reads the file unless something asks it to. Without this,
running locally outside Docker silently skipped .env entirely and the Spotify
features degraded to "not configured" with no obvious cause.

load_dotenv() deliberately does not override variables already set in the real
environment (that's its default). Docker-compose and CI values therefore win
over anything in a stray local file, which is the precedence you want.
"""
from dotenv import load_dotenv

load_dotenv()
