# Factorio Discord Bot Dockerfile
#
# This Dockerfile sets up the environment for the Factorio Discord Bot.
#
# Author: xPrimeTime
# Date: 7/19/2024
# Version: 1.3
# License: MIT

FROM python:3.9-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

CMD ["python", "bot.py"]
