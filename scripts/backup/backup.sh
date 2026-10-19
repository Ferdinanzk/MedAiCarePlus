#!/bin/sh
set -eu
umask 077
export TZ=Asia/Taipei

mkdir -p /backups/nightly /backups/weekly /ledger
touch /ledger/deletion_ledger.jsonl

backup_once() (
    set -eu
    stamp=$(date +%F)
    target="/backups/nightly/$stamp"
    stage=$(mktemp -d /backups/nightly/.pending.XXXXXX)
    weekly_tmp="/backups/weekly/.$stamp.tar.gpg.tmp"
    trap 'rm -rf "$stage"; rm -f "$weekly_tmp"' EXIT
    if [ ! -d "$target" ]; then
        pg_dump -Fc --file="$stage/database.dump"
        tar -C /gallery -cf "$stage/gallery.tar" .
        mv "$stage" "$target"
    fi
    if [ "$(date +%u)" = 7 ] && [ ! -f "/backups/weekly/$stamp.tar.gpg" ]; then
        if [ -z "${BACKUP_PASSPHRASE:-}" ]; then
            echo "Weekly backup failed: BACKUP_PASSPHRASE is empty" >&2
            exit 1
        fi
        # Materialise the bundle so tar failures cannot be hidden by a pipeline.
        mkdir -p "$stage"
        tar -C "$target" -cf "$stage/weekly.tar" database.dump gallery.tar
        gpg --batch --yes --pinentry-mode loopback --passphrase-fd 3 \
            --symmetric --cipher-algo AES256 --output "$weekly_tmp" "$stage/weekly.tar" 3<<EOF
$BACKUP_PASSPHRASE
EOF
        mv "$weekly_tmp" "/backups/weekly/$stamp.tar.gpg"
    fi
    echo "Backup completed: $stamp"
)

prune_backups() {
    # ISO dates sort chronologically; keep at most 14 nightly / 4 weekly sets.
    find /backups/nightly -mindepth 1 -maxdepth 1 -type d -name '20??-??-??' |
        sort -r | tail -n +15 | while IFS= read -r path; do rm -rf "$path"; done
    find /backups/weekly -maxdepth 1 -type f -name '20??-??-??.tar.gpg' |
        sort -r | tail -n +5 | while IFS= read -r path; do rm -f "$path"; done
    # Expire old copies even if subsequent backups have failed.
    find /backups/nightly -mindepth 1 -maxdepth 1 -type d -name '20??-??-??' -mtime +13 -exec rm -rf {} \;
    find /backups/weekly -maxdepth 1 -type f -name '20??-??-??.tar.gpg' -mtime +27 -delete
    find /backups/nightly -mindepth 1 -maxdepth 1 -type d -name '.pending.*' -mtime +0 -exec rm -rf {} \;
    find /backups/weekly -maxdepth 1 -type f -name '.*.tar.gpg.tmp' -mtime +0 -delete
}

backup_due() {
    # $1 today (YYYY-MM-DD), $2 the time (HH:MM). At 02:30, and once after 02:30 while today's nightly folder is
    # missing: a start later in the day catches up (the PC was off or Docker down at 02:30; 15-16 Oct 2026 have no
    # backup), and so does a loop whose 60 s sleeps stepped over the 02:30 minute.
    [ "$last_attempt" != "$1" ] || return 1
    [ "$2" = "02:30" ] && return 0
    hour=${2%:*}
    minute=${2#*:}
    [ $(( ${hour#0} * 60 + ${minute#0} )) -gt 150 ] && [ ! -d "/backups/nightly/$1" ]
}

last_attempt=""
while :; do
    prune_backups
    today=$(date +%F)
    if backup_due "$today" "$(date +%H:%M)"; then
        last_attempt=$today
        # Run outside a conditional so set -e remains effective in the subshell.
        set +e
        backup_once
        status=$?
        set -e
        if [ "$status" -ne 0 ]; then
            echo "Backup failed (exit $status); previous completed copies retained" >&2
        fi
        prune_backups
    fi
    sleep 60
done
