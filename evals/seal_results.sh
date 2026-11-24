#!/usr/bin/env bash
# Seal evaluation outputs before they leave the runner.
#
# The repository is public, so its Actions artifacts and logs are readable by
# anyone. Transcripts, tool responses and the captured app prompt hold the
# owner's live league context (roster, league members' Sleeper names, picks).
# They are bundled, encrypted with a fresh random AES-256 key, and that key is
# wrapped with evals/results_pubkey.pem (RSA-OAEP-SHA256). Only the holder of
# the matching private key, which is never committed, can read them. The plaintext
# is deleted before upload. ledger.json (spend and counts only, no league data)
# is copied out unencrypted so cost can be checked at a glance.
#
# Usage: seal_results.sh <plaintext_dir> <upload_dir>
set -euo pipefail
in="$1"; out="$2"
pub="$(dirname "$0")/results_pubkey.pem"
mkdir -p "$out"
umask 077
key="$(mktemp)"
trap 'rm -f "$key"' EXIT
openssl rand -hex 32 > "$key"
tar czf - -C "$in" . | openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt -pass file:"$key" -out "$out/results.tgz.enc"
openssl pkeyutl -encrypt -pubin -inkey "$pub" -pkeyopt rsa_padding_mode:oaep -pkeyopt rsa_oaep_md:sha256 \
  -in "$key" -out "$out/results.key.enc"
[ -f "$in/ledger.json" ] && cp "$in/ledger.json" "$out/ledger.json"
rm -rf "$in"
echo "sealed: $(ls "$out" | tr '\n' ' ')"
