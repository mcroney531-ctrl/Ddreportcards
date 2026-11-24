#!/usr/bin/env bash
# Reverse of seal_results.sh, run only where the private key lives (never CI).
# Usage: unseal_results.sh <private_key.pem> <sealed_dir> <out_dir>
set -euo pipefail
priv="$1"; in="$2"; out="$3"
mkdir -p "$out"
umask 077
key="$(mktemp)"
trap 'rm -f "$key"' EXIT
openssl pkeyutl -decrypt -inkey "$priv" -pkeyopt rsa_padding_mode:oaep -pkeyopt rsa_oaep_md:sha256 \
  -in "$in/results.key.enc" -out "$key"
openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -pass file:"$key" -in "$in/results.tgz.enc" | tar xzf - -C "$out"
