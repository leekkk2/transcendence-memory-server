FROM python:3.11-slim
WORKDIR /app
COPY scripts/ ./scripts/
RUN pip install --no-cache-dir \
    fastapi uvicorn httpx requests numpy \
    lancedb pyarrow lightrag-hku raganything
EXPOSE 8711
CMD ["uvicorn", "task_rag_server:app", "--app-dir", "scripts", "--host", "0.0.0.0", "--port", "8711"]
