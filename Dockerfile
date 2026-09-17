# Reproducible environment for maritime-fraud-signals.
#
# This is a data-science pipeline, not a web service: the image just bundles
# Python, the pinned dependencies and the project's own code, so that ingestion,
# processing, detectors and tests behave identically on any machine.
#
# `data/` and `outputs/` are never baked in -- they are gitignored, can be large,
# and are meant to be mounted as volumes at run time so results land on the host.
#
# Build:
#   docker build -t maritime-fraud-signals .
#
# Run the test suite (default command):
#   docker run --rm -v "$(pwd)/data:/app/data" -v "$(pwd)/outputs:/app/outputs" maritime-fraud-signals
#
# Drop into a shell instead (e.g. to run an ingestion module):
#   docker run --rm -it \
#     -v "$(pwd)/data:/app/data" \
#     -v "$(pwd)/outputs:/app/outputs" \
#     -v "$(pwd)/.env:/app/.env:ro" \
#     maritime-fraud-signals bash
#   $ python -m ingest.dma
#
# The AISStream key (and any future secret) lives in `.env` on the host and is
# mounted read-only at run time -- it is never copied into the image.

FROM python:3.12-slim

WORKDIR /app

# System build tools some wheels (e.g. lightgbm) may need to compile against;
# removed from the final layer to keep the image slim.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Project code only. data/ and outputs/ are excluded via .dockerignore and are
# mounted as volumes instead -- see the run examples above. viz/ is a static
# deck.gl frontend built for GitHub Pages, not part of the Python pipeline
# this image reproduces, so it is intentionally left out.
COPY ingest/ ingest/
COPY process/ process/
COPY pipeline/ pipeline/
COPY detect/ detect/
COPY features/ features/
COPY model/ model/
COPY report/ report/
COPY tests/ tests/
COPY docs/ docs/
COPY tasks.json .
COPY README.md .

# Placeholders so ingestion/processing have somewhere to write when a host
# volume isn't mounted (e.g. a quick `docker run` without -v).
RUN mkdir -p data/raw data/interim data/processed outputs

# Running the test suite is the most useful default for a portfolio project:
# it proves the environment is reproducible without requiring any data mount
# or secrets. Override the command (e.g. `bash`, `python -m ingest.dma`) for
# interactive work.
CMD ["pytest"]
