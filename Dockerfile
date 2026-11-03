FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY daemon/ daemon/
COPY bin/ bin/
COPY extension/ extension/
COPY config/ config/

RUN useradd --create-home braind \
 && mkdir -p /data/state \
 && chown -R braind:braind /data /app
USER braind

# Runtime secrets and state live in /data (mounted volume), never in the image:
#   /data/seed           BIP-39 mnemonic, mode 0600
#   /data/walletd.token  caller token, mode 0600
#   /data/state/         brain policy + intent/audit logs
ENV BRAIND_SEED_FILE=/data/seed \
    BRAIND_TOKEN_FILE=/data/walletd.token \
    BRAIND_STATE_DIR=/data/state

EXPOSE 18923 18924

CMD ["python", "daemon/walletd.py"]
