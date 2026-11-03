FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy dependency specifications
COPY requirements.txt pyproject.toml ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source code
COPY . .

# Expose ports
EXPOSE 8000 8001

# Default command: start ApexResolve API
CMD ["uvicorn", "apex_resolve.main:app_factory", "--factory", "--host", "0.0.0.0", "--port", "8000"]
