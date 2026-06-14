FROM ubuntu:24.04

# Install ffmpeg, python3, pip
RUN apt-get update && apt-get install -y \
    ffmpeg \
    python3 \
    python3-pip \
    && rm -rf /var/lib/apt/lists/*

# Create app directory
WORKDIR /app

# Copy requirements and install
COPY requirements.txt .
RUN pip3 install --break-system-packages -r requirements.txt

# Copy application files
COPY app.py .
COPY templates/ ./templates/
RUN mkdir -p static

# Create directories for frames and videos
RUN mkdir -p /app/frames /app/videos

# Expose port
EXPOSE 5000

# Run the app
CMD ["python3", "app.py"]
