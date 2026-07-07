FROM python:3.12-slim

WORKDIR /app

# ponytail: install deps first (cached), then copy code
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./
COPY static/ static/
COPY templates/ templates/

# Runtime data — mounted as volumes at container start
#   AllPrintings.sqlite
#   embeddings/
#   images/
#   eval_reports/
#   .secret_key

VOLUME ["/app/embeddings", "/app/images", "/app/eval_reports"]

EXPOSE 5000

CMD ["python", "app.py"]
