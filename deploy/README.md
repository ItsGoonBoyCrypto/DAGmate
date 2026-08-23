# Deploying DAGmate to dagmate.org

Three processes behind nginx on one host. Only nginx faces the internet.

    internet ──443──> nginx ──> 127.0.0.1:8800  site backend (API + frontend)
                                       │
                                       ├──> 127.0.0.1:8910  Kaspa sidecar ──> public node
                                       └──> 127.0.0.1:8901  alerts bot ──> Telegram

The sidecar is **never** proxied. It holds the arbiter keys and has no auth of
its own beyond not being reachable — a location block pointing at it would put
an unauthenticated settlement signer on the public internet.

DAGmate runs no Kaspa node. The sidecar resolves a public one from the
community pool and health-checks it on every connection (see spec §3.0).

## First run

Everything below is on the server, as root.

**1. Users, directories, and where the secrets live**

Three service users, one per process, so the key-holding sidecar, the web
backend, and the alerts bot cannot read each other's secrets or databases — an
RCE in the internet-facing bot must not be a path to the arbiter seed:

    for u in dagmate-svc dagmate-site dagmate-bot; do adduser --system --group --home /opt/dagmate "$u"; done
    mkdir -p /opt/dagmate /etc/dagmate /var/lib/dagmate/site /var/lib/dagmate/bot
    chown dagmate-site:dagmate-site /var/lib/dagmate/site
    chown dagmate-bot:dagmate-bot   /var/lib/dagmate/bot
    chmod 700 /var/lib/dagmate/site /var/lib/dagmate/bot

The databases live under `/var/lib/dagmate/<proc>` and the secrets in
`/etc/dagmate` — both outside `/opt/dagmate`, the deploy root that gets
`rsync --delete`d on every push. The sidecar writes nothing, so it has no state
dir. The code in `/opt/dagmate` stays owned by **root**: no service user can
rewrite the code it executes under `Restart=always`.

**2. Runtimes**

    apt install -y nodejs npm python3-venv nginx certbot python3-certbot-nginx
    python3 -m venv /opt/dagmate/venv

Node 18+ (the Kaspa WASM SDK needs modern `globalThis`/WASM support) and
Python 3.10+.

**3. First code push** — from your machine, not the server:

    DAGMATE_DEPLOY_HOST=root@<ip> deploy/deploy.sh

It will fail at the health check on a first run because there is no env file
yet. That is expected; carry on.

**4. Generate the seed — ON THE SERVER — into its own credential file**

    cd /opt/dagmate/service && node tools/genseed.mjs

This prints the master mnemonic and the derived operating address. The mnemonic
is the one secret in the system: anyone holding it can co-sign any settlement.
Put ONLY the mnemonic phrase into a root-owned credential file — the sidecar
receives it through systemd's credential store, never as an env var (so it
can't be read from /proc/<pid>/environ even by a same-box attacker):

    umask 077 && printf '%s' 'the twelve or twenty-four words' > /etc/dagmate/mnemonic
    chown root:root /etc/dagmate/mnemonic && chmod 0400 /etc/dagmate/mnemonic

Generate it here rather than on a laptop, because a phrase generated elsewhere
has already been through a clipboard, a shell history, and possibly a
scrollback that syncs to someone else's cloud. Never reuse a phrase from
another project — sharing one means a compromise of either is a compromise of
both.

Back it up offline. If it is lost, every open escrow can still be reclaimed by
its own depositor after 14 days (that is the point of the CLTV branch), but no
match can ever be settled again.

**5. Env files** — one per process, each readable only by its own user. NONE of
them holds the mnemonic (that's the credential file above):

    install -o root -g dagmate-svc  -m 0640 /opt/dagmate/deploy/service.env.example /etc/dagmate/service.env
    install -o root -g dagmate-site -m 0640 /opt/dagmate/deploy/site.env.example    /etc/dagmate/site.env
    install -o root -g dagmate-bot  -m 0640 /opt/dagmate/deploy/bot.env.example     /etc/dagmate/bot.env
    $EDITOR /etc/dagmate/service.env      # network / node (no secret)
    $EDITOR /etc/dagmate/site.env         # webhook secret, public URL, bot handle
    $EDITOR /etc/dagmate/bot.env          # bot token, webhook secret

The seed lives only in `/etc/dagmate/mnemonic` (0400 root), reached by the
sidecar through `LoadCredential=` — the site and bot users can't read it, and
it's not in any process's environment, so an RCE in any service can't lift the
arbiter seed. The webhook secret is shared between `site.env` and `bot.env`: the
**same** value in both, at least 32 chars (`openssl rand -hex 32`) — the
processes refuse to start on a shorter one. Then clear the scrollback the
mnemonic was printed into (`clear && history -c`, or just close the session).

**6. Fund the operating address** with a small KAS float — it pays the network
fee on any tx the sidecar sends from it (and, if you turn on `DAGMATE_ANCHOR_MOVES`
in `site.env`, one dust anchor per move). Check the balance at the address
`genseed.mjs` printed; if it shows nothing after you have sent to it, the phrase
in the env file is not the phrase the address came from.

**7. Units**

    cp /opt/dagmate/deploy/dagmate-*.service /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable --now dagmate-service dagmate-site dagmate-bot
    systemctl status dagmate-service

**8. TLS and nginx**

Point `dagmate.org` and `www.dagmate.org` A records at the host first — certbot
proves control over the domain by being served on it.

    cp /opt/dagmate/deploy/nginx-dagmate.conf /etc/nginx/sites-available/dagmate
    ln -s /etc/nginx/sites-available/dagmate /etc/nginx/sites-enabled/dagmate
    rm -f /etc/nginx/sites-enabled/default
    certbot --nginx -d dagmate.org -d www.dagmate.org
    nginx -t && systemctl reload nginx

**9. Telegram**

    curl "https://api.telegram.org/bot<TOKEN>/setWebhook?url="   # clear any stale webhook

The bot uses long polling, so it needs no inbound port and no webhook.

## Subsequent deploys

    DAGMATE_DEPLOY_HOST=root@<ip> deploy/deploy.sh

Deploys the committed tree (`git archive HEAD`), not the working tree, and
refuses to run with uncommitted changes. What ships is a commit, so `git log -1`
on either side answers "what is actually running" — which a
copy-the-files-I-changed deploy cannot.

The sidecar restarts before the backend, because the backend calls routes on it.
The other order produces version skew, whose symptom is
`Kaspa service has no route /escrow/...`.

## Before the URL goes out

- [ ] `curl https://dagmate.org/api/meta` — `"devRoutes": false`, `"network":
      "mainnet"`, `"platformFeeBps": 0`. If `devRoutes` is true on a public
      host, `dev-mark-funded` will start matches nobody paid for.
- [ ] `curl https://dagmate.org/api/health` — `"service_ok": true`.
- [ ] `curl -sI http://<ip>:8800/api/health` from **off** the box — must fail.
      Same for `:8910` and `:8901`. If any answers, it is the API with TLS and
      the rate limits removed.
- [ ] Log in with a real wallet (Kasware or Kastle) and check the signature
      popup names `dagmate.org`.
- [ ] Play one gas-only match end to end. If you enabled `DAGMATE_ANCHOR_MOVES`,
      confirm the per-move anchor txids resolve on an explorer; if not, confirm
      the UI does not claim moves are anchored.
- [ ] Play one real-stake match end to end: deposit, play, settle, and check
      the winner received the whole pot minus network fee — no platform cut.
- [ ] Let one escrow go unfunded and confirm the funded side can reclaim after
      the deadline (the 14-day timelock makes this a slow test; the refusal
      path — "this pot can still be released to its winner" — is the part worth
      checking on day one).

## When something is wrong

    journalctl -u dagmate-service -f     # chain: escrow builds, broadcasts
    journalctl -u dagmate-site -f        # matches, deposits, clocks, settlement
    journalctl -u dagmate-bot -f         # alerts only; never blocks a match

`service_ok: false` in `/api/health` means the sidecar is down or no public
node passed the health gate. The site stays up deliberately — challenges, the
lobby, and the board keep working — but no deposit is seen and nothing settles
until it clears.
