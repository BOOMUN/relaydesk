FROM postgres:18-alpine

# Alpine publishes pgvector as a small PostgreSQL extension package.  The
# official postgres image installs PostgreSQL under /usr/local, while Alpine's
# package uses versioned /usr paths, so copy only the extension artifacts into
# the server paths reported by pg_config.
RUN apk add --no-cache postgresql-pgvector=0.8.1-r0 \
    && cp /usr/lib/postgresql18/vector.so /usr/local/lib/postgresql/vector.so \
    && cp /usr/share/postgresql18/extension/vector* /usr/local/share/postgresql/extension/
