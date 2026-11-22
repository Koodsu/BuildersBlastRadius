# Same image the organisers labelled in: its Python decides how sqlglot's tests behave.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONHASHSEED=0 OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 \
    SKIP_INTEGRATION=0 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1 PIP_ROOT_USER_ACTION=ignore
COPY requirements.lock /build/requirements.lock
RUN pip install -r /build/requirements.lock
# sqlglot at the pinned commit, with bytecode compiled now because the run's disk is read-only.
RUN python -c "import urllib.request,tarfile,io,os; c='eb11aec4be8cb2122afe713a4816662c3f0fb2fa'; d=urllib.request.urlopen('https://github.com/tobymao/sqlglot/archive/'+c+'.tar.gz').read(); tarfile.open(fileobj=io.BytesIO(d)).extractall('/build/x'); os.rename('/build/x/sqlglot-'+c,'/sg')" \
 && PYTHONDONTWRITEBYTECODE= python -m compileall -q /sg/sqlglot /sg/tests \
 && chmod -R a+rX /sg
COPY maps /app/maps
COPY brx /app/brx
COPY selector.py /app/selector.py
RUN chmod -R a+rX /app
CMD ["python", "/app/selector.py"]
