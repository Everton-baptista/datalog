FROM python:3.12-slim
WORKDIR /srv/datalog
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY datalog/ datalog/
RUN python -m pip install --no-cache-dir ".[sso,lineage]"
USER 10001:10001
EXPOSE 4310
CMD ["python", "-m", "datalog", "--bind", "0.0.0.0"]
