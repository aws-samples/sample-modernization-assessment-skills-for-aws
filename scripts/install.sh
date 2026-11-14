#!/bin/sh
# moda-skill installer. POSIX sh. Downloads (or takes --from) a release zip, verifies it against SHA256SUMS,
# unpacks it under ${XDG_DATA_HOME:-$HOME/.local/share}/moda-skill/versions/<version>/, points `current` at it
# and writes the launcher ~/.local/bin/moda-skill. Needs: sh, python3 (3.11+), and curl or wget unless --from.
#
#   sh install.sh                                           # latest release from the project's GitHub releases (see README for the one-line form)
#   sh install.sh --version 2.2.1
#   sh install.sh --from ./moda-skill-2.2.1.zip            # offline; SHA256SUMS next to the zip is used when present
#   MODA_SKILL_BASE_URL=https://dxxxx.cloudfront.net/moda-skill sh install.sh      # another host (e.g. CloudFront)
set -eu

REPO=${MODA_SKILL_REPOSITORY:-aws-samples/sample-modernization-assessment-skills-for-aws}
BASE_URL=${MODA_SKILL_BASE_URL:-}
VERSION=${MODA_SKILL_VERSION:-}
FROM=
YES=0
ALLOW_ROOT=${MODA_SKILL_ALLOW_ROOT:-0}
INSTALL_ROOT=${MODA_SKILL_INSTALL_ROOT:-${XDG_DATA_HOME:-$HOME/.local/share}/moda-skill}
BIN_DIR=${MODA_SKILL_BIN_DIR:-$HOME/.local/bin}

usage() {
  echo "Usage: install.sh [--version x.y.z] [--from <zip>] [--base-url <url>] [--yes] [--allow-root]" >&2
  exit 2
}
fail() { echo "ERROR $1" >&2; exit "${2:-1}"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --version) [ $# -ge 2 ] || usage; VERSION=$2; shift 2 ;;
    --from) [ $# -ge 2 ] || usage; FROM=$2; shift 2 ;;
    --base-url) [ $# -ge 2 ] || usage; BASE_URL=$2; shift 2 ;;
    --yes) YES=1; shift ;;
    --allow-root) ALLOW_ROOT=1; shift ;;
    -h|--help) usage ;;
    *) usage ;;
  esac
done

if [ "$(id -u)" = "0" ] && [ "$ALLOW_ROOT" != "1" ]; then
  fail "refusing to install as root; run as your own user, or pass --allow-root inside a container"
fi
command -v python3 >/dev/null 2>&1 || fail "python3 is required (3.11 or later)"
python3 - <<'PY' || fail "python3 3.11 or later is required"
import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)
PY

sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1" | awk '{print $1}'
  else python3 -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$1"
  fi
}
fetch() { # url dest
  if command -v curl >/dev/null 2>&1; then curl -fsSL "$1" -o "$2"
  elif command -v wget >/dev/null 2>&1; then wget -q "$1" -O "$2"
  else fail "curl or wget is required to download $1"
  fi
}

TMP=$(mktemp -d 2>/dev/null || mktemp -d -t moda)
trap 'rm -rf "$TMP"' EXIT

if [ -n "$FROM" ]; then
  [ -f "$FROM" ] || fail "no such file: $FROM"
  ZIP=$FROM
  SUMS=$(dirname "$FROM")/SHA256SUMS
  if [ ! -f "$SUMS" ]; then SUMS=; fi
else
  if [ -z "$BASE_URL" ]; then
    if [ -n "$VERSION" ]; then BASE_URL="https://github.com/$REPO/releases/download/v$VERSION"
    else BASE_URL="https://github.com/$REPO/releases/latest/download"; fi
  fi
  if [ -z "$VERSION" ]; then
    fetch "$BASE_URL/VERSION" "$TMP/VERSION" || fail "cannot read $BASE_URL/VERSION
  If the repository is private or you are offline, install from the release files you were given:
    sh install.sh --from ./moda-skill-<version>.zip      (SHA256SUMS in the same folder)
  or set MODA_SKILL_BASE_URL to a mirror that serves VERSION, the zip and SHA256SUMS."
    VERSION=$(tr -d ' \r\n' < "$TMP/VERSION")
  fi
  echo "$VERSION" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+([.-][0-9A-Za-z.]+)?$' || fail "unexpected version string: $VERSION"
  ZIP=$TMP/moda-skill-$VERSION.zip
  echo "Downloading moda-skill $VERSION from $BASE_URL" >&2
  fetch "$BASE_URL/moda-skill-$VERSION.zip" "$ZIP" || fail "download failed: $BASE_URL/moda-skill-$VERSION.zip
  If the repository is private or you are offline, install from the release files you were given:
    sh install.sh --from ./moda-skill-$VERSION.zip      (SHA256SUMS in the same folder)
  or set MODA_SKILL_BASE_URL to a mirror that serves VERSION, the zip and SHA256SUMS."
  fetch "$BASE_URL/SHA256SUMS" "$TMP/SHA256SUMS" || fail "download failed: $BASE_URL/SHA256SUMS"
  SUMS=$TMP/SHA256SUMS
fi

if [ -n "$SUMS" ]; then
  EXPECTED=$(grep " $(basename "$ZIP")\$" "$SUMS" | awk '{print $1}' | head -n 1)
  [ -n "$EXPECTED" ] || fail "$(basename "$ZIP") is not listed in SHA256SUMS"
  ACTUAL=$(sha256_of "$ZIP")
  [ "$EXPECTED" = "$ACTUAL" ] || fail "checksum mismatch for $(basename "$ZIP"): expected $EXPECTED, got $ACTUAL"
  echo "Checksum verified" >&2
else
  [ "$YES" = "1" ] || fail "no SHA256SUMS next to $ZIP; pass --yes to install an unverified local zip"
  echo "WARNING installing without checksum verification" >&2
fi

python3 - "$ZIP" "$TMP/unpacked" <<'PY'
import sys, zipfile, pathlib
zip_path, dest = sys.argv[1], pathlib.Path(sys.argv[2])
with zipfile.ZipFile(zip_path) as z:
    for name in z.namelist():
        p = pathlib.PurePosixPath(name)
        if p.is_absolute() or ".." in p.parts:
            sys.exit("zip contains an unsafe path: " + name)
    z.extractall(dest)
PY
SRC=$(find "$TMP/unpacked" -maxdepth 2 -name VERSION -print | head -n 1)
[ -n "$SRC" ] || fail "VERSION file not found inside the zip"
SRC=$(dirname "$SRC")
ZIP_VERSION=$(tr -d ' \r\n' < "$SRC/VERSION")
if [ -n "$VERSION" ] && [ "$VERSION" != "$ZIP_VERSION" ]; then fail "zip is version $ZIP_VERSION, expected $VERSION"; fi
VERSION=$ZIP_VERSION
[ -f "$SRC/scripts/moda_skill_cli.py" ] || fail "scripts/moda_skill_cli.py missing from the package"

DEST=$INSTALL_ROOT/versions/$VERSION
mkdir -p "$INSTALL_ROOT/versions" "$BIN_DIR"
if [ -d "$DEST" ]; then
  rm -rf "$DEST.old"; mv "$DEST" "$DEST.old"
fi
mv "$SRC" "$DEST"
rm -rf "$DEST.old"
chmod -R go-w "$DEST" 2>/dev/null || true
rm -rf "$INSTALL_ROOT/current"
printf '%s\n' "$VERSION" > "$INSTALL_ROOT/current"

cat > "$BIN_DIR/moda-skill" <<'LAUNCH'
#!/bin/sh
# moda-skill launcher: runs the active version's CLI with the same python3.
set -eu
ROOT=${MODA_SKILL_INSTALL_ROOT:-${XDG_DATA_HOME:-$HOME/.local/share}/moda-skill}
[ -f "$ROOT/current" ] || { echo "moda-skill: no active version under $ROOT; re-run install.sh" >&2; exit 1; }
VERSION=$(tr -d ' \r\n' < "$ROOT/current")
CLI=$ROOT/versions/$VERSION/scripts/moda_skill_cli.py
[ -f "$CLI" ] || { echo "moda-skill: version $VERSION is missing under $ROOT/versions; re-run install.sh" >&2; exit 1; }
exec python3 "$CLI" "$@"
LAUNCH
chmod 755 "$BIN_DIR/moda-skill"

echo "Installed moda-skill $VERSION to $DEST" >&2
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) echo "Add $BIN_DIR to PATH, e.g.:  export PATH=\"$BIN_DIR:\$PATH\"" >&2 ;;
esac
echo "Next: cd <your project> && moda-skill config --harness claude|codex|kiro && moda-skill doctor" >&2
