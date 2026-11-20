FROM python:3.14.7-slim@sha256:51dafde81dbdb6ebde285137a295cf18a47ca95234fe388a343719cb97305b3d
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 TZ=Asia/Shanghai HOME=/tmp
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir --require-hashes -r requirements.txt && useradd --uid 10001 --no-create-home app
COPY server.py movements.json ./
COPY xunji ./xunji
USER 10001:10001
CMD ["python", "server.py"]
