FROM python:3.12-slim

WORKDIR /app

# Instala dependências em camada separada para aproveitar cache do Docker.
# O volume bind-mount em runtime substitui /app, mas os pacotes ficam em
# /usr/local/lib/python3.12/site-packages — fora do mount, sempre disponíveis.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copia o código-fonte (sobrescrito pelo volume em desenvolvimento, mas
# necessário para builds autônomos / deploys sem bind-mount).
COPY . .

# Exec form garante que Python seja PID 1 e receba SIGTERM do Docker diretamente,
# permitindo o graceful shutdown implementado em main.py.
CMD ["python", "main.py"]
