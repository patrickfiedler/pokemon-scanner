# Deployment: VPS with Caddy + Let's Encrypt

## Install dependencies

```bash
sudo apt install tesseract-ocr python3-pip
pip install -r requirements.txt
git clone https://github.com/PokemonTCG/pokemon-tcg-data data/pokemon-tcg-data
python import_cards.py
```

## Caddyfile (auto HTTPS with Let's Encrypt)

```
yourcards.yourdomain.com {
    reverse_proxy localhost:8000
}
```

Start Caddy: `caddy run` or `sudo systemctl start caddy`

## systemd service

Create `/etc/systemd/system/pokemon-scanner.service`:

```ini
[Unit]
Description=Pokemon Card Scanner
After=network.target

[Service]
User=youruser
WorkingDirectory=/path/to/pokemon
Environment=SCANNER_PASSWORD=yourpassphrase
ExecStart=uvicorn src.backend.main:app --host 127.0.0.1 --port 8000
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now pokemon-scanner
```

## Update card data

```bash
cd data/pokemon-tcg-data && git pull && cd ../..
python import_cards.py
```
