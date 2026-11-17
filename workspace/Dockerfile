# Trading workspace server: standard-library Python, no dependencies.
#   docker build -t tj-workspace workspace && docker run -p 8765:8765 -e ALPACA_KEY_ID=... -e ALPACA_SECRET_KEY=... \
#     -e ANTHROPIC_API_KEY=... -e WORKSPACE_PASSWORD=... -v tjdata:/data tj-workspace
FROM python:3.12-slim
WORKDIR /app
COPY server ./server
COPY web ./web
ENV HOST=0.0.0.0 PORT=8765 DATA_DIR=/data PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8765
CMD ["python", "server/server.py"]
