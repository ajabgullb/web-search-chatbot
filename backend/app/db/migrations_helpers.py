"""Checks that migrations run before changing a database.

Kept out of the revision files so they can be tested directly and reused by a
later revision that faces the same hazard.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection

# Collations that sort by byte or code point under every C library, so a
# database using one is unaffected by a change of libc.
_LIBC_INDEPENDENT_COLLATIONS = frozenset({"C", "POSIX", "C.UTF-8", "C.utf8"})

REATTACHED_VOLUME_ERROR = (
    "Refusing to migrate: this database was initialised under musl libc "
    "(the old postgres:16-alpine image) and is now served by a glibc server "
    "(pgvector/pgvector). The two sort {collate!r} text differently, so its "
    "text indexes, including the unique index on users.email, may silently "
    "return wrong results. Do not reattach the old data volume: dump it with "
    "the old image and restore into a fresh volume, as described in "
    "docs/deployment.md (Moving to the pgvector image)."
)


def refuse_reattached_musl_volume(connection: Connection) -> None:
    """Stop if the data directory changed C library underneath its indexes.

    PostgreSQL records a database's collation version at creation. musl
    reports none, glibc reports its release, so a database created under
    musl and now running under glibc has a recorded version of NULL but an
    actual version that is not. PostgreSQL itself does not warn in that
    case, because it has nothing recorded to compare against.

    ICU collations and the byte-order ones do not depend on libc and pass.
    """
    row = connection.execute(
        text(
            # ::text because datlocprovider is the one-byte "char" type, which
            # asyncpg returns as bytes: b"c" never equals "c", and the check
            # would pass every database without looking at it.
            "SELECT datlocprovider::text AS datlocprovider, datcollate, datcollversion, "
            "pg_database_collation_actual_version(oid) AS actual_version "
            "FROM pg_database WHERE datname = current_database()"
        )
    ).one()
    if row.datlocprovider != "c" or row.datcollate in _LIBC_INDEPENDENT_COLLATIONS:
        return
    if row.datcollversion is None and row.actual_version is not None:
        raise RuntimeError(REATTACHED_VOLUME_ERROR.format(collate=row.datcollate))
