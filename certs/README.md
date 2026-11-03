# Company root certificates (only for networks that inspect HTTPS)

Corporate networks that inspect HTTPS traffic (Zscaler, Netskope and similar) re-sign every site with a company root
certificate. Windows and macOS trust it, but the Linux container doesn't, so the app can't reach Claude or Salesforce
and you see `CERTIFICATE_VERIFY_FAILED` (or, when building, `pip` fails with the same error).

**On a normal network, leave this folder empty.**

## Fix

1. Export the company root certificate into this folder as a PEM file ending in `.crt` (commands below).
2. Restart the app:
   - prebuilt image: `docker compose up -d` (the compose file mounts this folder at `/certs`, and the container trusts
     every `*.crt` in it when it starts)
   - `docker run`: add `-v "${PWD}/certs:/certs:ro"`
   - building from source: `docker compose up -d --build` also bakes the certificates into the image, which `pip`
     needs during the build

The log shows `Trusting 1 extra certificate(s) from /certs` when it worked (`docker compose logs`).

## Export the certificate

**Windows (PowerShell),** from this project folder. Change `Zscaler Root CA` to your proxy's certificate name
(see `certmgr.msc` > *Trusted Root Certification Authorities*):

```powershell
$c = Get-ChildItem Cert:\LocalMachine\Root, Cert:\CurrentUser\Root | Where-Object Subject -like '*Zscaler Root CA*' | Select-Object -First 1
"-----BEGIN CERTIFICATE-----`n$([Convert]::ToBase64String($c.RawData, 'InsertLineBreaks'))`n-----END CERTIFICATE-----" |
  Set-Content -Encoding ascii certs\company-root-ca.crt
```

**macOS (Terminal):**

```bash
security find-certificate -c "Zscaler Root CA" -p /Library/Keychains/System.keychain > certs/company-root-ca.crt
```

These are public CA certificates (no private keys), but they are git-ignored because they are specific to one network.
