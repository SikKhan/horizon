#!/usr/bin/env bash
# Game-derived folders sealed for a cache entry, and unsealed from one (.github/workflows/parity-hosted.yml).
#
#   VANILLA_DATA_PASSPHRASE=... bash tools/ci/sealed-cache.sh seal LABEL ARCHIVE [--exclude PATH]... FOLDER...
#   VANILLA_DATA_PASSPHRASE=... bash tools/ci/sealed-cache.sh unseal LABEL ARCHIVE [--exclude PATH]... STAGE
#
# A fork's pull request can restore the default branch's cache entries, and a branch's own copy of a workflow can
# name any key, so what is made from the vanilla data goes into a cache only as this archive:
# - a POSIX tar (nanosecond modification times: tools/parity_shim.py replays an oracle run only while its outputs
#   keep theirs), zstd, then the dataset's own encryption (openssl enc -aes-256-cbc -pbkdf2 -iter 600000 -md sha256,
#   the passphrase of horizon-rust/vanilla-data);
# - ARCHIVE.mac: HMAC-SHA256 over a version, LABEL and the whole ciphertext, keyed by PBKDF2 of the same passphrase
#   with a salt of its own, so the two keys are independent. openssl enc has no authenticated mode, and age reads a
#   passphrase only from a terminal.
# unseal checks the MAC before it decrypts anything, so an entry someone without the passphrase wrote, or one sealed
# under another LABEL, is never decrypted. It then unpacks into STAGE (a new folder under RUNNER_TEMP, laid out as
# from /), which the workflow copies into place. Whatever fails there (no tag, a wrong tag, a broken archive, a path
# outside the two folders below) is a warning: STAGE is removed and the run goes on cold, never fails for it.
#
# FOLDERs are absolute paths under the checkout or RUNNER_TEMP. An excluded PATH (a file or folder in them, such as
# the oracle shim's machine-local runtime memo) is never sealed, and unseal removes it from the stage should an
# entry hold it. The passphrase comes from this step's environment, is
# written to a private temporary file and unset at once, and that file goes before the script ends, whatever happens.
# The MAC key never reaches a command line: the runner's own Python derives it from the file. Only sizes, counts and
# seconds are printed, never a file name or the secret.
set -euo pipefail
umask 077
mode=${1:?seal or unseal}
label=${2:?label}
archive=${3:?archive}
shift 3
excluded=()
while [ "${1-}" = --exclude ]; do
  excluded+=("$(realpath -m "${2:?--exclude needs a path}")")
  shift 2
done
[[ "$label" =~ ^[a-z][a-z0-9-]*$ ]] || { echo "::error::bad label"; exit 2; }
: "${VANILLA_DATA_PASSPHRASE:?}" "${GITHUB_WORKSPACE:?}" "${RUNNER_TEMP:?}"
workspace=$(realpath -m "$GITHUB_WORKSPACE")
temp=$(realpath -m "$RUNNER_TEMP")
allowed() { case "$1/" in "$workspace"/?*|"$temp"/?*) return 0 ;; *) return 1 ;; esac; }
for path in "${excluded[@]}"; do allowed "$path" || { echo "::error::an excluded path must be under the checkout or RUNNER_TEMP"; exit 2; }; done

work=$(mktemp -d "$temp/sealed-cache.XXXXXX")
cleanup() { rm -rf "$work"; }
trap cleanup EXIT
printf '%s' "${VANILLA_DATA_PASSPHRASE//[$'\r\n']/}" > "$work/passphrase"
unset VANILLA_DATA_PASSPHRASE
cipher=(-aes-256-cbc -pbkdf2 -iter 600000 -md sha256 -pass "file:$work/passphrase")
started=$SECONDS

# mac FILE [TAG]: print FILE's hex tag under LABEL; with TAG (a file), exit 0 only when it holds FILE's tag (compared
# in constant time). Isolated (-I) and run from the private folder: no module of the checkout or the current folder
# can stand in for hashlib or hmac.
mac() {
  (cd "$work" && /usr/bin/python3 -I - "$work/passphrase" "$label" "$@" <<'EOF'
import hashlib, hmac, sys
passphrase, label, path, *expected = sys.argv[1:]
with open(passphrase, "rb") as file:
    key = hashlib.pbkdf2_hmac("sha256", file.read(), b"horizon-rust/horizon sealed-cache MAC key v1", 600000)
tag = hmac.new(key, b"horizon sealed-cache v1\0" + label.encode() + b"\0", "sha256")
with open(path, "rb") as file:
    for block in iter(lambda: file.read(1 << 20), b""):
        tag.update(block)
if not expected:
    print(tag.hexdigest())
else:
    with open(expected[0], "rb") as file:
        sys.exit(0 if hmac.compare_digest(file.read().strip(), tag.hexdigest().encode()) else 1)
EOF
  )
}

case "$mode" in
  seal)
    members=()
    for folder in "$@"; do
      folder=$(realpath -m "$folder")
      allowed "$folder" || { echo "::error::a sealed folder must be under the checkout or RUNNER_TEMP"; exit 2; }
      [ -e "$folder" ] && members+=("${folder#/}")
    done
    [ "${#members[@]}" -gt 0 ] || { echo "nothing to seal"; exit 0; }
    mkdir -p "$(dirname "$archive")"
    # Member names are relative to / and anchored: an excluded folder is left out with everything under it.
    patterns=()
    for path in "${excluded[@]}"; do patterns+=("--exclude=${path#/}"); done
    tar -C / --format=posix --pax-option=delete=atime,delete=ctime --anchored "${patterns[@]}" \
      -cf - -- "${members[@]}" |
      zstd -q -T0 -3 |
      openssl enc -e "${cipher[@]}" > "$work/archive"
    mac "$work/archive" > "$work/archive.mac"
    mv "$work/archive" "$archive"
    mv "$work/archive.mac" "$archive.mac"
    echo "sealed ${#members[@]} folders, $(du -sk "${members[@]/#//}" | awk '{s+=$1} END {print s}') KiB," \
      "into $(( $(stat -c %s "$archive") / 1024 )) KiB in $((SECONDS - started)) s"
    ;;
  unseal)
    stage=$(realpath -m "${1:?stage}")
    case "$stage/" in "$temp"/?*) ;; *) echo "::error::the stage must be under RUNNER_TEMP"; exit 2 ;; esac
    [ ! -e "$stage" ] || { echo "::error::the stage already exists"; exit 2; }
    discard() { echo "::warning::$label cache entry discarded ($1): this run starts without it"; rm -rf "$stage"; exit 0; }
    [ -f "$archive" ] || { echo "no $label cache entry"; exit 0; }
    [ -f "$archive.mac" ] || discard "no authentication tag"
    mac "$archive" "$archive.mac" || discard "its authentication tag does not match"
    mkdir -p "$stage"
    openssl enc -d "${cipher[@]}" < "$archive" 2> /dev/null | zstd -dq -T0 2> /dev/null |
      tar -C "$stage" -xv --no-same-owner > "$work/members" 2> /dev/null || discard "it did not unpack"
    # Member names go to a private file (never the log) and must all lie in the two folders seal takes them from.
    outside=0
    while IFS= read -r member; do allowed "/${member%/}" || outside=$((outside + 1)); done < "$work/members"
    [ "$outside" -eq 0 ] || discard "$outside paths outside the checkout and RUNNER_TEMP"
    for path in "${excluded[@]}"; do
      if [ -e "$stage$path" ] || [ -L "$stage$path" ]; then
        rm -rf "${stage:?}$path"
        echo "::warning::the $label cache entry held an excluded path: removed before it is placed"
      fi
    done
    echo "unsealed $label: $(( $(stat -c %s "$archive") / 1024 )) KiB in $((SECONDS - started)) s"
    ;;
  *) echo "::error::seal or unseal"; exit 2 ;;
esac
