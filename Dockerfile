# Use official lightweight Python image
FROM python:3.11-slim

# Allow statements and log messages to immediately appear in the Knative logs
ENV PYTHONUNBUFFERED True

# Set working directory
WORKDIR /app

# Copy local code to the container image
COPY . ./

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Cloud Run injects the PORT environment variable (default 8080)
# We use that variable to start uvicorn
CMD exec uvicorn backend.main:app --host 0.0.0.0 --port $PORT
