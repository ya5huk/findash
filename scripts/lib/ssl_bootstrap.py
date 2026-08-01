"""Point Python at a system CA bundle when its own store is missing.

The python.org macOS framework build compiles in an OpenSSL cert path that
does not exist until its "Install Certificates.command" is run once; until
then every urllib HTTPS call fails with SSL: CERTIFICATE_VERIFY_FAILED.
Scripts that fetch over HTTPS call ensure_ca_bundle() before their first
request: when the interpreter has no usable default CA store and the
environment does not already provide one, SSL_CERT_FILE is pointed at the
first well-known system bundle that exists. Healthy interpreters (Linux
distro Pythons, Homebrew builds) already resolve a store, so for them this
is a no-op. run_daily.sh applies the same guard at the shell level so ad-hoc
python invoked by the skills mid-flow is covered too.
"""

import os
import ssl

# Well-known system CA bundle locations, checked in order. /etc/ssl/cert.pem is
# the standard macOS location (kept current by OS updates); the rest cover the
# common Linux layouts so the guard degrades gracefully elsewhere.
SYSTEM_BUNDLES = (
    "/etc/ssl/cert.pem",                   # macOS, Alpine, Arch, BSDs
    "/etc/ssl/certs/ca-certificates.crt",  # Debian, Ubuntu
    "/etc/pki/tls/certs/ca-bundle.crt",    # Fedora, RHEL
    "/etc/ssl/ca-bundle.pem",              # openSUSE
)


def ensure_ca_bundle(environ=os.environ, verify_paths=None,
                     candidates=SYSTEM_BUNDLES):
    """Set SSL_CERT_FILE in ``environ`` if the interpreter would otherwise
    have no CA store. Returns the bundle path that was set, or None when
    nothing needed doing (store already usable, env already set, or no bundle
    found). OpenSSL reads the variable each time a default SSL context loads
    its certs, so calling this before the first request is enough; the setting
    also propagates to child processes."""
    if environ.get("SSL_CERT_FILE") or environ.get("SSL_CERT_DIR"):
        return None
    if verify_paths is None:
        verify_paths = ssl.get_default_verify_paths()
    if verify_paths.cafile or verify_paths.capath:
        return None
    for bundle in candidates:
        if os.path.isfile(bundle):
            environ["SSL_CERT_FILE"] = bundle
            return bundle
    return None
