# Built by the scorer on linux/amd64; the run has no network. Same base image
# as the one the commits were labeled in.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

ENV PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# sqlglot's source at the pinned commit: the selector diffs each patched file
# against it to find which functions, classes or modules changed.
ARG SQLGLOT_COMMIT=eb11aec4be8cb2122afe713a4816662c3f0fb2fa
RUN python -c "import io, tarfile, urllib.request; \
data = urllib.request.urlopen('https://codeload.github.com/tobymao/sqlglot/tar.gz/${SQLGLOT_COMMIT}', timeout=120).read(); \
tar = tarfile.open(fileobj=io.BytesIO(data)); \
members = [m for m in tar.getmembers() if m.isfile() and '/' in m.name]; \
[setattr(m, 'name', m.name.split('/', 1)[1]) for m in members]; \
tar.extractall('/app/src', members=members, filter='data')" \
 && test -f /app/src/sqlglot/parser.py

# The coverage map built from the pinned commit (mapper/ shows how), unpacked
# here so the run does not pay to decompress it.
COPY map.pkl.gz type-map.json attributes.json selector.py run.py /app/
RUN python -c "import gzip, shutil; shutil.copyfileobj(gzip.open('/app/map.pkl.gz'), open('/app/map.pkl', 'wb'))" \
 && rm /app/map.pkl.gz \
 && python -m compileall -q /app/selector.py \
 && chmod -R a+rX /app

ENV BLAST_MAP=/app/map.pkl
CMD ["python", "-S", "/app/run.py"]
