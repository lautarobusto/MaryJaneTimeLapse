FROM rclone/rclone:1.68.2 AS rclone

FROM ubuntu:24.04

# Install ffmpeg, python3, pip
RUN apt-get update && apt-get install -y \
    ffmpeg \
    python3 \
    python3-pip \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=rclone /usr/local/bin/rclone /usr/local/bin/rclone

# Create app directory
WORKDIR /app

# Copy requirements and install
COPY requirements.txt .
RUN pip3 install --break-system-packages -r requirements.txt

# Copy application files
COPY app.py .
COPY templates/ ./templates/

# spool/work are tmpfs mounts in docker-compose; data holds small settings files
RUN mkdir -p /app/spool /app/work /app/data && chown -R 1000:1000 /app

ENV RCLONE_CONFIG=/config/rclone/rclone.conf \
    PYTHONUNBUFFERED=1
USER 1000:1000

# Expose port
EXPOSE 5000

# Run the app
CMD ["python3", "app.py"]
