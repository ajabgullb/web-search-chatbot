#!/bin/sh
# Tell nginx which peers may report the client's address.
#
# The API rate-limits login, registration, and password reset per client IP,
# and learns that IP only from the X-Forwarded-For header this nginx sends. If
# nginx passed along whatever the client wrote there, any client could claim a
# fresh address on every request and never be limited.
#
# So nginx works out the real address itself: X-Forwarded-For is believed only
# when the connection comes from a proxy listed in TRUSTED_PROXY_CIDRS (the TLS
# terminator, a load balancer, Cloudflare's ranges). Walking the header from
# the right, the first address that is *not* a trusted proxy is the client.
# default.conf then forwards exactly that one address to the API.
#
# Empty means "nothing is in front of nginx": the TCP peer is the client.
set -eu

out=/etc/nginx/conf.d/00-real-ip.conf
cidrs=$(printf '%s' "${TRUSTED_PROXY_CIDRS:-}" | tr ',' ' ')

if [ -z "$(printf '%s' "$cidrs" | tr -d ' ')" ]; then
    echo "real-ip: TRUSTED_PROXY_CIDRS is empty; the TCP peer is taken as the client" >&2
    : > "$out"
    exit 0
fi

{
    echo "# Generated at container start from TRUSTED_PROXY_CIDRS."
    for cidr in $cidrs; do
        # Addresses and prefixes only: a typo must stop the container rather
        # than reach nginx, and nothing else may be smuggled into the config.
        if ! printf '%s' "$cidr" | grep -Eq '^[0-9A-Fa-f:.]+(/[0-9]{1,3})?$'; then
            echo "real-ip: '$cidr' in TRUSTED_PROXY_CIDRS is not an address or CIDR" >&2
            exit 1
        fi
        echo "set_real_ip_from $cidr;"
    done
    echo "real_ip_header X-Forwarded-For;"
    echo "real_ip_recursive on;"
} > "$out"
echo "real-ip: trusting X-Forwarded-For from: $cidrs" >&2
