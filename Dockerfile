# Use an official, lightweight Python runtime
FROM python:3.11-slim-buster

# Prevent Python from writing .pyc files and force stdout/stderr buffering to be unbuffered
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Set the working directory inside the container
WORKDIR /app

# Install system dependencies required for database connections (libpq for PostgreSQL)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy only the requirements first to leverage Docker's caching layer
COPY requirements.txt /app/

# Install python dependencies
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Copy the rest of your local application code into the container
COPY . /app/

# Expose the internal port to match our Gunicorn config
EXPOSE 8080

# Run the application using our production server configuration
CMD ["gunicorn", "-c", "gunicorn.conf.py", "Flask_Forecast_app:app"]
