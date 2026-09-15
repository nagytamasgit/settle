# Deploying settle-app to a VPS

From a bare Ubuntu or Debian box to a working instance with HTTPS. Budget half
an hour, most of it waiting for DNS.

This deploys the **app** (`settle-app`), not the engine. The engine is a library
and a CLI and does not need deploying — `pip install settle-engine` and you have
it.

---

## Before anything else: two things worth knowing

**The licence.** settle is [PolyForm Noncommercial 1.0.0](../LICENSE): free for
individuals, study, research, teaching, nonprofits and government. **Running it
for a business — including your own — needs a commercial licence.** A deploy
guide is exactly where somebody decides to put this in front of a finance team,
so it belongs at the top rather than in a footnote. See
[`COMMERCIAL.md`](../COMMERCIAL.md); terms are negotiable and the address is
nagytmas@gmail.com.

**What this stores, and who can read it.** Every run keeps the invoice ledger
and bank statement you uploaded, plus the results, on disk, **indefinitely by
default**. That is customer names, invoice numbers, amounts and payment
references. Anyone with the password can read all of it and delete any of it,
and because there is one shared password rather than user accounts, the access
log can record *when* and *from where* but never *who*. If that is not an
acceptable trade for your situation, stop here and use the CLI instead — it
keeps nothing.

Set `SETTLE_WEB_RETENTION_DAYS` if you would rather runs expired.

---

## 1. Prepare the box

1 vCPU and 1 GB of RAM is enough; 2 GB is comfortable. Any provider will do.

```bash
# As root, on a fresh box.
adduser deploy && usermod -aG sudo deploy

# SSH keys only. Set PasswordAuthentication no and PermitRootLogin no.
sudoedit /etc/ssh/sshd_config && systemctl restart ssh

ufw allow OpenSSH && ufw allow 80 && ufw allow 443 && ufw enable
apt update && apt install -y unattended-upgrades && dpkg-reconfigure -plow unattended-upgrades
```

Then Docker, from Docker's own repository rather than the distribution's:

```bash
curl -fsSL https://get.docker.com | sh
usermod -aG docker deploy
```

Log out and back in as `deploy` so the group takes effect.

## 2. Point DNS at it, and wait

Create an `A` record (and `AAAA` if you have IPv6) for the hostname you want,
pointing at the box's address. **Do this before the first start.** Caddy asks
Let's Encrypt for a certificate the moment it comes up, and that fails if the
name does not yet resolve to this machine.

```bash
dig +short settle.example.com     # must return your box's IP before continuing
```

## 3. Install

```bash
git clone https://github.com/nagytamasgit/settle.git
cd settle/app

mkdir -p secrets
openssl rand -base64 24 > secrets/settle_password
chmod 600 secrets/settle_password

echo "SETTLE_DOMAIN=settle.example.com" > .env

docker compose up -d
```

The password is in `secrets/settle_password`. Read it once, put it in a password
manager, and give it to the people who need it. It is mounted as a Docker
secret rather than an environment variable so it does not appear in
`docker inspect` or in the process list.

## 4. Check it actually worked

```bash
docker compose ps           # both services up; settle shows (healthy)
curl -I https://settle.example.com
```

You want `HTTP/2 200` and a `strict-transport-security` header. Then confirm the
app is **not** reachable except through Caddy:

```bash
docker compose ps           # the settle service must show no published ports
curl --max-time 5 http://YOUR_IP:8000/healthz    # must fail to connect
```

Open the site, sign in, and click **Try the sample**. If you get a results page
with numbers on it, you are done.

## 5. If you want it properly locked down

A shared password on the public internet is a reasonable floor, not a ceiling.
For a single finance team the better posture costs nothing:

> Put the box on [Tailscale](https://tailscale.com) or WireGuard, drop 80 and
> 443 from the public firewall, and reach the app over the private network. The
> password then becomes defence in depth rather than the only thing between a
> stranger and your customers' bank details.

If you do that you can skip Caddy entirely and run the app on `127.0.0.1`
reached through the tunnel — but note the app refuses to start bound to a public
interface without a password, and that refusal is deliberate. Do not work around
it.

---

## The model feature, if you turn it on

`SETTLE_WEB_MODEL_MAPPER=1` lets a model propose a mapping when an uploaded
file's column headers do not match settle's. It is **off by default** and the app
is complete without it.

What it sends: **column headers only** — `Invoice No`, `Client Name`, and so on.
Not amounts, not customer names, not payment references. That is the default and
it is the setting to keep. `SETTLE_WEB_MODEL_SAMPLES=1` additionally sends a few
example values, which improves the guess slightly and sends real customer data
to a third party. Think about that one properly before enabling it.

The model only ever *proposes*. A human confirms the mapping on screen before
anything is reconciled, and there is no code path that skips that screen.

Enabling it needs an `ANTHROPIC_API_KEY` in the environment and the `model`
extra installed.

---

## Backups

**Do not back up the database by copying the file.** SQLite in WAL mode keeps
recent commits outside the main file, so `cp` while the app is running produces
a torn database — something you discover on the day you need it. Use the online
backup API, which is what `settle-app backup` does.

Nightly, from the host:

```bash
#!/bin/sh
# /home/deploy/backup-settle.sh
set -eu
cd /home/deploy/settle/app
DAY=$(date +%F)
docker compose exec -T settle settle-app backup "/data/backups/settle-$DAY.db"
docker compose exec -T settle tar -czf "/data/backups/runs-$DAY.tgz" -C /data runs
find /data/backups -name '*.db' -mtime +30 -delete
```

Then get them **off the box**, encrypted — the archives contain customer
financial data:

```bash
restic -r s3:... backup /var/lib/docker/volumes/app_settle-data/_data/backups
```

### Rehearse the restore, once

The restore is the half nobody tests, and an untested backup is a hope.

```bash
docker compose down
# Replace /data/settle.db and /data/runs from the archive.
docker compose up -d
# Open an old run and check it renders.
```

Do it once now, while nothing is wrong.

---

## Updating

```bash
cd ~/settle && git pull
cd app
docker compose exec -T settle settle-app backup /data/backups/pre-upgrade.db
docker compose build
docker compose up -d
docker compose logs -f settle
```

Migrations are forward-only and run before the app binds its socket, so a failed
migration is a container that exits rather than one serving a schema it cannot
read. **There is no downgrade.** Rolling back means restoring
`pre-upgrade.db` and checking out the previous commit — which is why the backup
is the first step rather than the last.

---

## When something is wrong

**`docker compose logs settle`** first, always.

*The container exits immediately with "refusing to bind".* No password is set
and the app is bound to a public interface. That is the startup refusal working.
Check `secrets/settle_password` exists and is not empty.

*The container exits with a schema version error.* The data volume was written
by a newer build than the image you are running. Check out the matching commit,
or restore the pre-upgrade backup.

*Caddy will not get a certificate.* DNS is not pointing here yet, or 80/443 are
closed. `dig +short yourdomain` and `ufw status` in that order.

*Permission denied on `/data`.* The volume is not writable by uid 10001. This
looks like a database error and gets diagnosed as one; it is not.

*Someone forgot the password.* Write a new one into
`secrets/settle_password` and `docker compose restart settle`. Every existing
session is invalidated, by design — the session key is derived from the
password, so rotating it is the one revocation mechanism this app has.

*A statement is too large and the upload is refused.* That cap is deliberate:
reconciliation runs inside the HTTP request. Use the CLI for a ledger that big:

```bash
docker compose run --rm settle settle run invoices.csv bank.csv --out results/
```
