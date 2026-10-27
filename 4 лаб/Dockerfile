FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY common ./common
COPY provider ./provider
COPY search ./search
CMD ["uvicorn", "search.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
