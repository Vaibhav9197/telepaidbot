FROM python:3.11-slim-bookworm

# Overridable at build time so the image's clock matches wherever it is deployed;
# compose can also override it at run time.
ARG TZ=Asia/Kolkata
ENV TZ=${TZ}
ARG DEBIAN_FRONTEND=noninteractive

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    git build-essential tzdata ffmpeg libssl-dev libffi-dev && \
    ln -snf "/usr/share/zoneinfo/$TZ" /etc/localtime && echo "$TZ" > /etc/timezone && \
    rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir -U pip wheel==0.45.1

WORKDIR /app
COPY requirements.txt /app
RUN pip install -U -r requirements.txt

COPY . /app

CMD ["python3", "main.py"]
