FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN groupadd --gid 1000 agentdesk \
    && useradd --uid 1000 --gid agentdesk --create-home agentdesk

WORKDIR /app

COPY requirements.txt ./requirements.txt
RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements.txt

COPY --chown=agentdesk:agentdesk backend ./backend
COPY --chown=agentdesk:agentdesk frontend/dist ./frontend/dist

RUN mkdir -p /app/data && chown agentdesk:agentdesk /app/data

USER agentdesk

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
