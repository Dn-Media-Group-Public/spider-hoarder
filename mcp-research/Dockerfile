FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Cookie-consent network blocklist, fetched once at build time (no runtime
# network dependency): Fanboy's Cookiemonster list.
RUN python -c "import requests; open('cmp_blocklist.txt', 'wb').write(requests.get('https://secure.fanboy.co.nz/fanboy-cookiemonster.txt', headers={'User-Agent': 'Mozilla/5.0'}).content)"

COPY . .

EXPOSE 8080
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
