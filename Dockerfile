FROM python:3.11-slim

WORKDIR /app

# System deps for psycopg2, pdfplumber, reportlab, and (Phase 8,
# CLARIVA-DOCGEN-SPEC-001) headless LibreOffice for DOCX->PDF conversion —
# see utils/pdf_convert.py. libreoffice-writer alone (rather than the full
# libreoffice meta-package) keeps the image smaller while covering the one
# conversion filter this app actually uses.
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    poppler-utils \
    libreoffice-writer \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
