FROM python:3.12-slim

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

# Runtime data — mounted as volumes at container start
#   AllPrintings.sqlite
#   embeddings/
#   images/
#   eval_reports/
#   .secret_key

VOLUME ["/app/embeddings", "/app/images", "/app/eval_reports"]

EXPOSE 5000 8765 8000

CMD ["./docker-entrypoint.sh"]
