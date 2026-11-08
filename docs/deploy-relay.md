# Deploying the relay on a VPS (systemd + Caddy)

Untested on a live host: these files were written against the code and its
tests, not run under systemd. Do the testnet pass first (step 6).

Two machines: **relay VPS** (public, no secrets of value) and your **signer**
(existing install). Never put the relay on the signer.

## A. Relay VPS (Ubuntu/Debian, as root)

```bash
# 1. code (copy the repo, or only relay/ and deploy/)
mkdir -p /opt/braind && cp -r relay deploy /opt/braind/   # run from the repo root
apt-get install -y python3 caddy                           # relay itself needs no pip packages

# 2. secrets: two different keys
install -m 600 -o root -g root deploy/braind-relay.env.example /etc/braind-relay.env
python3 -c "import secrets;print(secrets.token_urlsafe(32))"   # -> RELAY_AGENT_KEY  (for Muse)
python3 -c "import secrets;print(secrets.token_urlsafe(32))"   # -> RELAY_PULLER_KEY (signer only)
nano /etc/braind-relay.env     # paste both keys, set RELAY_PUBLIC_URL to your https URL

# 3. service
cp deploy/braind-relay.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now braind-relay
journalctl -u braind-relay -n 20 --no-pager      # expect: "braind-relay on 127.0.0.1:18930 ..."

# 4. HTTPS (point your domain's DNS A record at the VPS first)
cp deploy/Caddyfile /etc/caddy/Caddyfile && nano /etc/caddy/Caddyfile   # set your domain
systemctl reload caddy
ufw allow 80,443/tcp && ufw allow OpenSSH && ufw enable                 # 18930 stays closed

# 5. check
curl https://relay.example.com/healthz          # {"ok": true}
curl https://relay.example.com/openapi.json | head -c 200
```

## B. Signer machine

```bash
umask 077; printf '%s' '<RELAY_PULLER_KEY>' > ~/.braind/relay_puller.key
sudo cp deploy/braind-puller.service /etc/systemd/system/
sudo nano /etc/systemd/system/braind-puller.service   # user, paths, RELAY_URL; keep or drop --auto-deliver
sudo systemctl daemon-reload && sudo systemctl enable --now braind-puller
journalctl -u braind-puller -f
```

## 6. Testnet pass (do this before any mainnet chain)

```bash
# from anywhere, with the AGENT key:
curl -s https://relay.example.com/v1/proposals -H "Authorization: Bearer $AGENT_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"chain":"sepolia","to":"<your allowlisted or self address>","value":"0.0001","reason":"deploy test"}'
# -> {"id":"prop_...","status":"pending"}; the puller log shows it queued as an intent.
# on the signer:
python3 bin/brain.py list
python3 bin/brain.py approve --id <intent_id> --hash <intent_hash> --by me
# the puller delivers (if --auto-deliver) and the status becomes "executed":
curl -s https://relay.example.com/v1/proposals/<id> -H "Authorization: Bearer $AGENT_KEY"
```

Then add the connector in Muse (docs/muse-connector.md, section 3).

## Operations

- **Rotate the agent key** (leak, or you stop using Muse): edit
  `/etc/braind-relay.env`, `systemctl restart braind-relay`, update Muse.
- **Rotate the puller key:** same file, then rewrite `relay_puller.key` on the signer and restart the puller.
- **Kill switch:** `systemctl stop braind-relay` stops all new proposals instantly; anything already approved on the signer is unaffected.
- **Enable a mainnet:** add it to `RELAY_CHAINS`, make sure the Brain policy has a low `cap_wei` and a tight allowlist, and `BRAIND_APPROVAL_KEY_FILE` is set on the signer.
- **Backups:** the relay DB only holds proposals; losing it loses history, not funds.
