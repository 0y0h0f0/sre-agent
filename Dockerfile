FROM python:3.13-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1

COPY . /app
RUN python -m pip install --upgrade pip \
    && python -m pip install -e ".[dev]" \
    && python -m pip install kubernetes

CMD ["uvicorn", "apps.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
