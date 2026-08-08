# Deploying to a free Oracle Cloud VPS

Moving the bot off a home connection is the largest speed change available to
it — but not for the reason it first appears.

Telegram throttles throughput **per connection**, not per account, which is why
`DOWNLOAD_WORKERS` exists at all: you don't outrun the throttle, you open N
connections and each gets its own allowance. That premise depends entirely on
the link keeping those connections alive, and on a domestic line it does not.
The measurement in [fastdl.py](../helpers/fastdl.py#L13) is stark — **0.44 MB/s
on 4 workers against 1.31 MB/s on one**, because every reset costs a retry.
Parallelism is currently net negative.

A datacenter link doesn't drop them, so 8 workers finally multiply instead of
collapsing, and the upload half stops being capped by an asymmetric domestic
uplink. That is where the gain lives. Proximity to the DC adds perhaps 10% on
top — worth having, not the headline.

Oracle Cloud's **Always Free** tier is the only free offering with enough
bandwidth to matter. The relevant numbers:

| Resource | Always Free allowance |
| --- | --- |
| Compute | 4 ARM cores (Ampere A1), 24 GB RAM, running 24/7 |
| Storage | 200 GB block volume |
| Egress | 10 TB/month |
| Duration | indefinite, not a 12-month trial |

Egress is the one that rules out the alternatives: Google Cloud's free tier
allows 1 GB/month, and AWS/Azure allow 100 GB for twelve months only. Here, only
uploads to Telegram count as egress — downloads are ingress and free — so the
practical ceiling is around 10 TB of uploaded media per month.

## 1. Pick the right region — this cannot be changed later

Your **home region is permanent**, chosen during signup, and Always Free
resources only exist there. Getting it wrong means a new account.

Each worker awaits one 1 MiB chunk at a time, so the round trip is dead time
added to every chunk. From India to Singapore that is ~70 ms against a throttled
transfer of several hundred — a garnish. From a US or EU region it is ~200 ms,
which costs a third of your throughput on every chunk for the life of the
account. Pick correctly because picking wrongly is irreversible, not because the
right pick is dramatic.

This session lives on **DC5, Singapore**, so the home region should be
**Singapore (ap-singapore-1)**. Confirm before signing up:

```sh
python deploy/whichdc.py
```

Re-run this if you ever swap in a session string from a different account.

## 2. Create the account

Sign up at [cloud.oracle.com](https://cloud.oracle.com). A card is required for
identity verification and takes a small temporary hold; Always Free resources
are not charged. Set the home region to Singapore.

## 3. Create the instance

Compute → Instances → **Create instance**:

- **Image**: Canonical Ubuntu 24.04
- **Shape**: Change shape → Ampere → `VM.Standard.A1.Flex` → **4 OCPUs, 24 GB**
- **Boot volume**: tick "specify a custom boot volume size" and set **200 GB**.
  A separate block volume would need manual iSCSI attachment; one large boot
  volume avoids that and uses the same free allowance.
- **SSH keys**: paste your public key
- **Networking**: accept the defaults. The bot makes only outbound connections,
  so no ingress rules are needed and nothing has to be exposed.

If you get **"Out of host capacity"** — common for the free ARM shape — try a
different availability domain, or retry over a few hours. Upgrading the account
to pay-as-you-go improves availability substantially and keeps the Always Free
resources free.

## 4. Bootstrap it

SSH in as `ubuntu`, then:

```sh
curl -fsSL https://raw.githubusercontent.com/Vaibhav9197/telepaidbot/main/deploy/bootstrap.sh | bash
```

That installs Docker, clones the repo to `/opt/rcdl`, creates
`/var/lib/rcdl/tmp` for in-flight files, scaffolds `config.env` from
[config.env.oracle](config.env.oracle), and installs the `rcdl` systemd service.
It is safe to re-run and never overwrites an existing `config.env`.

Then fill in the four secrets and start it:

```sh
nano /opt/rcdl/config.env      # API_ID, API_HASH, BOT_TOKEN, SESSION_STRING
sudo systemctl start rcdl
systemctl status rcdl
```

First start builds the image on 4 ARM cores and takes a few minutes.

## 5. Ramp the workers up — don't start at the ceiling

A datacenter link will happily hold 16 connections open, so it is tempting to set
`DOWNLOAD_WORKERS=16` and be done. The link is not the binding constraint.
Telegram rate-limits `upload.GetFile` **per account**, and that limit does not
care that you moved to better hardware. Push past it and you get FloodWait; keep
pushing from a brand-new datacenter IP on a *user* session rather than a bot one,
and the exposure is no longer a 30-second wait — it is a limited account.

So the template starts at 8, matching the tuned laptop baseline, and you earn
your way up.

**The multiplication to watch.** `DOWNLOAD_WORKERS` is per file, not global —
[fastdl.py](../helpers/fastdl.py#L292) opens that many media sessions inside
*each* concurrent download. Concurrent streams are therefore
`MAX_CONCURRENT_DOWNLOADS × DOWNLOAD_WORKERS`. The template pins
`MAX_CONCURRENT_DOWNLOADS=1` so that product equals `DOWNLOAD_WORKERS` and a
single number governs your exposure. Raising both is how you end up with 32
streams thinking you configured 16.

**The ramp.** The benchmark prints a `floods` column per worker count, so it
answers this directly rather than by feel:

```sh
cd /opt/rcdl
docker compose -f docker-compose.yml -f deploy/docker-compose.vps.yml \
  exec media_bot python benchmark_speed.py <url>
```

Point it at a reasonably large post, then read two columns together:

| `floods` | `MB/s` vs the row above | What it means |
| --- | --- | --- |
| 0 | still climbing | headroom — go one step higher |
| 0 | flat | link ceiling reached; more workers gain nothing |
| >0 | anything | **you are over the account limit — back off a step** |

Step `8 → 12 → 16`, and stop at the last row with `floods` at 0 *and* a real
speed gain. Flat-with-zero-floods is your answer too — extra connections past
that point are pure risk for no throughput.

Then `sudo systemctl restart rcdl`.

**Keep `SERVER_SIDE_COPY=1`.** Unprotected posts are copied inside Telegram, so
they cost no bandwidth and issue no `GetFile` calls at all — the one speed win
with zero FloodWait exposure.

## What the code already does for you

Worth knowing so you can tell a tuning problem from a real one:

- Temporary media sessions reuse the warmed session's auth key
  (`export_authorization=False`), so opening 16 connections does **not** mean 16
  `auth.ExportAuthorization` calls — that particular flood vector is already
  closed.
- `sleep_threshold=30` lets Pyrogram absorb any FloodWait of 30s or less
  silently, and `fetch_chunk` retries a chunk three times before giving up on the
  file.
- Every FloodWait is counted in `RETRY_STATS` and logged, so `/logs` will show
  you whether a slow batch was throttling or just a slow file.

None of that raises the account limit — it only keeps you from losing a download
to a brief brush with it. A steady stream of FloodWait warnings in `/logs` means
lower `DOWNLOAD_WORKERS`, not a retry problem.

## Operating it

```sh
sudo systemctl restart rcdl     # after editing config.env
sudo systemctl reload rcdl      # rebuild after a git pull
journalctl -u rcdl -n 50        # service-level problems
cd /opt/rcdl && docker compose -f docker-compose.yml \
  -f deploy/docker-compose.vps.yml logs -f    # the bot's own output
```

Note that `config.env` is gitignored, so it never arrives from a `git pull` —
it lives only on the VPS. `/logs` and `/stats` work the same as they do locally.

## Two things to expect

**A login notification.** Starting the session from a Singapore IP for the first
time may prompt a Telegram security notice, and occasionally a re-auth. Have the
account's phone available for the first start.

**Idle reclamation.** Oracle reclaims Always Free instances that sit idle for
long stretches. An actively used bot will not trip this, but a box left untouched
for weeks can be.
