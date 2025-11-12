# Use an official Python runtime as a parent image
FROM python:3.9-slim

# Install build dependencies first
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc python3-dev && \
    rm -rf /var/lib/apt/lists/*

# Set the working directory in the container
WORKDIR /app

# Copy the current directory contents into the container at /app
COPY . .

# Install any needed packages specified in requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Make port 5001 available to the world outside this container
EXPOSE 5001

# Define environment variable (can be overridden by docker-compose)
ENV FLASK_APP=Influx2.py

# Run the application when the container launches
CMD ["flask", "run", "--host", "0.0.0.0", "--port", "5001"]