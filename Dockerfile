FROM python:3.12-slim

# ponytail: UID/GID as build args so container writes are owned by the host user,
# not root.  Defaults to 1000:1000 (typical single-user Linux / macOS).
ARG UID=1000
ARG GID=1000

WORKDIR /app

# ponytail: install deps first (cached by layer), then copy code
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./
COPY static/ static/
COPY templates/ templates/
COPY docker-entrypoint.sh ./

# Runtime directories — created empty so VOLUME mounts work cleanly.
# Images dirs nested inside too (app caches from Scryfall on first hit).
RUN mkdir -p embeddings images/normal images/large eval_reports && \
    chmod +x docker-entrypoint.sh

# Create non-root user matching the host user so bind-mount writes are properly owned.
# --no-create-home is omitted so the home dir exists — sentence-transformers and
# other ML libraries write to ~/.cache for model downloads.
RUN groupadd --gid $GID mtg 2>/dev/null || true && \
    useradd --uid $UID --gid $GID -m --shell /bin/bash mtg 2>/dev/null || true && \
    chown -R mtg:mtg /app
USER mtg

# Runtime data — mounted as volumes at container start
#   AllPrintings.sqlite
#   embeddings/
#   images/
#   eval_reports/
#   .secret_key

VOLUME ["/app/embeddings", "/app/images", "/app/eval_reports"]

EXPOSE 5000 8765 8000

CMD ["./docker-entrypoint.sh"]
