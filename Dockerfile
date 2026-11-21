# The scorer builds this on linux/amd64 and runs it with no network. The
# digest is the image every commit was labeled in: its Python and SQLite
# decide which sqlglot tests run.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS base
# A fixed hash seed makes fingerprints of import-time state comparable between
# the machine that built the maps and the run.
ENV PYTHONDONTWRITEBYTECODE=1 SKIP_INTEGRATION=0 PYTHONHASHSEED=0 \
    OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1
COPY requirements.lock /build/requirements.lock
RUN pip install --no-cache-dir -r /build/requirements.lock
# sqlglot's own repository at the pinned commit.
ADD https://github.com/tobymao/sqlglot/archive/eb11aec4be8cb2122afe713a4816662c3f0fb2fa.tar.gz /build/sqlglot.tar.gz
RUN mkdir /sg && tar -xzf /build/sqlglot.tar.gz -C /sg --strip-components=1 \
    && rm /build/sqlglot.tar.gz \
    && python -m compileall -q /sg/sqlglot /sg/tests \
    && chmod -R a+rX /sg

FROM base AS final
COPY br /app/br
# Maps built from the pinned commit by `make maps` (br/tracer.py, br/aggregate.py,
# br/basestate.py): which lines and class attributes each test touches, which
# files it reads, and a fingerprint of sqlglot's import-time state.
COPY maps.tar.xz /build/maps.tar.xz
RUN mkdir /app/maps && python -m tarfile -e /build/maps.tar.xz /app/maps \
    && rm /build/maps.tar.xz \
    && python -m compileall -q /app/br \
    && chmod -R a+rX /app
COPY selector.py /app/selector.py
# BR_PROBE=1: a test too expensive to check in full (the three that make up half
# the suite's run time) is judged on a part of itself once a dozen cheaper tests
# that run the same changed code have passed. Set it to 0 to schedule such a test
# whenever it could be affected: slower on average, but never a judgment call.
ENV BR_PROBE=1
CMD ["python", "/app/selector.py"]
