# Instinct Portal

A JSON-RPC 2.0 endpoint that an Instinct AI agent calls over HTTPS. Each request carries a short-lived bearer token that you mint on your own machine and paste into the chat.

The instructions for the agent itself are in [docs/instinct-agent.md](docs/instinct-agent.md).

## Layout

| Path | Purpose |
|---|---|
| `src/main.py` | The server. Serves `POST /rpc`. Holds public keys only. |
| `src/keygen.py` | Generates an ed25519 key pair. |
| `src/mint.py` | Signs tokens with a private key. |
| `src/tokens.py` | Token format and key-file naming, shared by the three scripts above. |
| `deploy/instinct-portal.service` | systemd unit for running the server as a service. |

## Keys and tokens

There are two lifetimes:

- **Key:** set when you run `keygen.py` (`--ttl`, default 24h). One key signs any number of tokens until it expires.
- **Token:** set when you run `mint.py` (`--ttl`, default 10m). It is cut short if the key expires first.

Keys are files named `<created>_<expires>.private.pem` and `<created>_<expires>.public.pem`, with UTC timestamps such as `20261116T132342Z`.

- **The server needs only the `.public.pem` file.** It picks up new keys without a restart.
- **Deleting a `.public.pem` revokes that key immediately.** Every token it signed stops working.
- **The `.private.pem` stays on the machine where you mint.** Keygen creates it readable only by you.

Tokens are single-use by default. The server counts uses in a SQLite file, so a used token stays used after a restart.

### Generate a key

```sh
uv run src/keygen.py --ttl 7d
```

This prints the private key's path and deletes expired key pairs in the same directory. Choose the lifetime by how often you want to rotate keys: tokens are what need to be short-lived, and key expiry only limits the damage if a private key leaks.

Use `--keys-dir DIR` to write somewhere other than `keys/`. On the service setup below, that is `/etc/instinct-portal/keys`.

If you mint on a different machine from the server, copy only the public half across:

```sh
scp keys/<created>_<expires>.public.pem laptop:/etc/instinct-portal/keys/
```

### Mint a token

```sh
uv run src/mint.py keys/<created>_<expires>.private.pem                  # single-use, 10 min
uv run src/mint.py keys/<created>_<expires>.private.pem --uses 5 --ttl 1h
uv run src/mint.py keys/<created>_<expires>.private.pem --unlimited --ttl 30m
```

Only the token goes to stdout, so it can be piped to the clipboard: `| pbcopy` on macOS, `| wl-copy` on Ubuntu (Wayland). Details such as the token ID and expiry go to stderr.

To mint again, run the same command. When the key has expired, `mint.py` prints `key ... expired ...; run keygen.py`. Generate a new key, and copy its public half to the server if minting happens elsewhere.

### Shortcut

Add this to `~/.bashrc` (or `~/.zshrc`) to mint with the newest key without typing its filename. Adjust the two paths for the machine:

```sh
mint() {
  local dir=/etc/instinct-portal/keys project=/opt/instinct-portal key
  key=$(ls "$dir"/*.private.pem 2>/dev/null | tail -1)
  [ -n "$key" ] || { echo "no private key in $dir; run keygen" >&2; return 1; }
  uv run --project "$project" "$project/src/mint.py" "$key" "$@"
}
```

Filenames start with the creation timestamp, so `tail -1` selects the newest key. Arguments are passed through: `mint --uses 5 --ttl 1h | wl-copy`.

## Running locally

```sh
uv sync
uv run src/keygen.py
uv run src/main.py                 # 127.0.0.1:8000
```

Check it with a token from `mint.py`:

```sh
curl localhost:8000/rpc -H "Authorization: Bearer $T" \
  -d '{"jsonrpc":"2.0","method":"ping","id":1}'
# {"jsonrpc":"2.0","id":1,"result":"pong"}
```

Server options: `--host`, `--port`, `--keys-dir`, `--db`. Keep `--host` at `127.0.0.1` and put HTTPS in front with a tunnel; tokens sent over plain HTTP can be read in transit.

## Running as a service (Ubuntu)

The service runs as a temporary unprivileged user (systemd `DynamicUser`) that cannot read `/home`. A compromise of this internet-facing process then cannot reach your files. The consequence is that the code, its Python interpreter and the keys must all live outside `/home`:

| What | Where |
|---|---|
| Code and `.venv` | `/opt/instinct-portal` |
| Python interpreter | `/opt/instinct-portal/.python` |
| Public keys (and your private keys, readable only by you) | `/etc/instinct-portal/keys` |
| Use-count database | `/var/lib/instinct-portal/used.sqlite` (created by systemd) |

### 1. Copy the code

From the development machine, in the project directory:

```sh
ssh laptop 'sudo install -d -o $USER /opt/instinct-portal'
rsync -a --exclude-from=.gitignore ./ laptop:/opt/instinct-portal/
```

### 2. Install dependencies

On the server:

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh        # if uv isn't installed
cd /opt/instinct-portal
UV_PYTHON_INSTALL_DIR=/opt/instinct-portal/.python uv sync
readlink -f .venv/bin/python                            # must start with /opt/instinct-portal/.python
```

`UV_PYTHON_INSTALL_DIR` is required. Without it, uv installs Python under `~/.local/share/uv`, the venv's `python` links into `/home`, and the service fails with `status=203/EXEC` because it cannot see `/home`.

If `.venv` already exists from a run without this setting, delete it first (`rm -rf .venv`). uv keeps reusing an existing venv's interpreter, so running `uv sync` again does not fix the link.

### 3. Create the keys directory and a key

```sh
sudo install -d -o $USER -m 755 /etc/instinct-portal/keys
uv run src/keygen.py --keys-dir /etc/instinct-portal/keys --ttl 7d
```

Or generate the key on another machine and copy only the `.public.pem` here.

### 4. Install and start the service

```sh
sudo cp deploy/instinct-portal.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now instinct-portal
systemctl status instinct-portal
```

### 5. Expose it publicly with Tailscale Funnel

```sh
sudo tailscale up                          # if not already on the tailnet
sudo tailscale funnel --bg 8000            # survives reboots; prints the public URL
```

The first run may print a link to enable HTTPS and Funnel in the Tailscale admin console.

Funnel decrypts HTTPS on this machine, so Tailscale's relays never see tokens or payloads. Turn it off with `sudo tailscale funnel reset`.

Put the URL, with `/rpc` appended, into [docs/instinct-agent.md](docs/instinct-agent.md) as `PORTAL_URL`. The hostname is listed in public certificate logs, so expect automated scanners to start sending requests within minutes. They get 404s; only `POST /rpc` exists.

### 6. Keep a laptop online

Closing the lid suspends the machine. In `/etc/systemd/logind.conf`, set these two lines, then reboot:

```ini
HandleLidSwitch=ignore
HandleLidSwitchExternalPower=ignore
```

Restarting `systemd-logind` instead of rebooting can end the desktop session.

### Logs

```sh
journalctl -u instinct-portal -f -o cat                          # live
journalctl -u instinct-portal -f -o cat | grep -v uvicorn.access # hide scanner 404s
journalctl -u instinct-portal -b                                 # since boot, with starts and crashes
```

Payloads are logged only for authenticated requests. Authentication failures log the reason, never the token or the payload.

### Updating

```sh
# from the development machine
rsync -a --exclude-from=.gitignore ./ laptop:/opt/instinct-portal/
ssh laptop 'cd /opt/instinct-portal && UV_PYTHON_INSTALL_DIR=/opt/instinct-portal/.python uv sync && sudo systemctl restart instinct-portal'
```

### Troubleshooting

| Symptom | Cause |
|---|---|
| `status=203/EXEC` | systemd could not run the interpreter. Usually `.venv/bin/python` links into `/home`; see step 2. Otherwise check permissions with `namei -l "$(readlink -f /opt/instinct-portal/.venv/bin/python)"`: every directory needs `x` for others. |
| `status=31/SYS` | A system call was blocked by `SystemCallFilter` in the unit file. |
| Every request returns `unknown_key` | No valid `.public.pem` in `/etc/instinct-portal/keys`, or the token was signed by a key that is not there. |
| Every request returns `key_expired` | Generate a new key. |

To test whether the service user can run the interpreter at all:

```sh
sudo systemd-run --pty --wait -p DynamicUser=yes -p ProtectHome=yes \
  /opt/instinct-portal/.venv/bin/python -c 'print("ok")'
```
