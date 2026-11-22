# The scorer builds this on linux/amd64 and runs it with no network. The
# digest is the image the organizers labeled every commit in: its Python and
# SQLite decide which sqlglot tests run, so keep it.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

# Pristine sqlglot at the pinned commit (GitHub archive: allowed build download).
ARG SQLGLOT_SHA=eb11aec4be8cb2122afe713a4816662c3f0fb2fa
COPY tools/fetch_src.py /tmp/fetch_src.py
RUN python /tmp/fetch_src.py "$SQLGLOT_SHA" /opt/sqlglot && rm /tmp/fetch_src.py

# The canary runner imports the sqlglot test modules, which need these (versions from requirements.lock).
RUN pip install --no-cache-dir duckdb==2.0.0 numpy==2.5.5 pandas==3.1.1

COPY selector.py /app/selector.py
COPY blast /app/blast
COPY maps /app/maps
COPY tools/fp_baseline.py /app/tools/fp_baseline.py
# Precompile everything, then fingerprint the pristine import in THIS environment (seconds).
RUN python -m compileall -q /app /opt/sqlglot/sqlglot /opt/sqlglot/tests \
 && python /app/tools/fp_baseline.py /opt/sqlglot /app/fp
ENV PYTHONDONTWRITEBYTECODE=1
CMD ["python", "/app/selector.py"]
