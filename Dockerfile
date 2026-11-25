FROM python:3.12-slim
WORKDIR /srv/tmd_cde
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV TMD_CDE_DATA_DIR=/srv/tmd_cde/data
EXPOSE 8000
CMD ["sh", "-c", "python cli.py init && python cli.py serve --host 0.0.0.0 --port 8000"]
