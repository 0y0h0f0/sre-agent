FROM python:3.13-slim

WORKDIR /app

RUN pip install fastapi uvicorn sentence-transformers torch --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple

COPY deploy/bge_zh_server.py /app/server.py

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8083"]
