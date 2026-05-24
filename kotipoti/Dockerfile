FROM python:3.11-slim

# Install build deps for numpy/pandas on ARM
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Default: run the bot. Override CMD in compose for dashboard.
CMD ["python", "main.py"]
