#!/bin/sh
# Trust extra root certificates mounted at /certs when the container starts.
# Needed on networks that inspect HTTPS (Zscaler, Netskope...) when you use the prebuilt image, which can't include
# your company's certificate. Certificates baked in at build time (certs/*.crt) are already in the system bundle.
set -e
if ls /certs/*.crt >/dev/null 2>&1; then
  bundle=/tmp/ca-bundle.crt
  cat /etc/ssl/certs/ca-certificates.crt /certs/*.crt > "$bundle"
  export SSL_CERT_FILE="$bundle" REQUESTS_CA_BUNDLE="$bundle" NODE_EXTRA_CA_CERTS="$bundle"
  echo "Trusting $(ls /certs/*.crt | wc -l) extra certificate(s) from /certs"
fi
exec "$@"
