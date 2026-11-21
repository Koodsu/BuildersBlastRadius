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
# Maps built from the pinned commit by `make maps`: which lines and class
# attributes each test touches, which files it reads and depends on, how long
# it takes, and a fingerprint of sqlglot's import-time state.
COPY maps.tar.xz /build/maps.tar.xz
RUN mkdir /app/maps && python -m tarfile -e /build/maps.tar.xz /app/maps \
    && rm /build/maps.tar.xz \
    && python -m compileall -q /app/br \
    && chmod -R a+rX /app
# Fingerprints of the fixture entries behind the tests that work through a
# fixture one entry at a time, made by `python -m br.entries` (part of `make maps`).
COPY unit_entries.json /app/maps/unit_entries.json
# What the executor's pooled tests hand on from the optimizer, and the lines
# that run after it, made by `python -m br.stages` (part of `make maps`).
COPY stage_prints.json /app/maps/stage_prints.json
COPY selector.py /app/selector.py
# BR_PROBE=1: the two tests that cost the most to run (recorded at about 21 s
# and 29 s) are left out when every other test that runs the changed statement
# has been run here and has passed. Evidence, not proof. With 0 they are run
# here, or scheduled, whenever they could be affected.
# BR_CPU_FACTOR: CPU seconds charged for work estimated at one second. Scored at
# 0.4 the selector spent 89.7 s of CPU to save 46 s of simulated time.
ENV BR_PROBE=1 BR_CPU_FACTOR=1.0
# BR_DUAL=1: when the edit is one side-effect-free expression, the old and the
# new form are evaluated side by side in every candidate that is cheap to run.
# If they never differ, the candidates that are dear to run are left out.
# Evidence, not proof. Set it to 0 to check or schedule those as before.
ENV BR_DUAL=1
# Experiment: evidence taken further. With BR_PROBE_ALL=1, once every candidate
# that was cheap enough to check has passed (at least BR_PROBE_ALL_OTHERS of
# them), the candidates that were only scheduled because checking them costs
# too much are left out as well; BR_PROBE_OTHERS is the same count for the two
# giants. Measured on generated edits only: see the README before relying on it.
ENV BR_PROBE_ALL=1 BR_PROBE_ALL_OTHERS=1 BR_PROBE_OTHERS=1
CMD ["python", "/app/selector.py"]
