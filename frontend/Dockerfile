# syntax=docker/dockerfile:1.7
# ---------------------------------------------------------------------------
# Frontend: static files behind nginx.
# ---------------------------------------------------------------------------
# The frontend is plain HTML/CSS/JS with no build step, so the image is just
# nginx and a handful of files. No bundler, no node_modules, no toolchain to
# keep patched.

FROM nginx:1.27-alpine

LABEL org.opencontainers.image.title="harmony-frontend" \
      org.opencontainers.image.description="Static frontend for the Harmony social network"

# Replace the stock site with a hardened configuration.
RUN rm -f /etc/nginx/conf.d/default.conf
COPY nginx/nginx.conf /etc/nginx/nginx.conf
COPY nginx/security-headers.conf /etc/nginx/conf.d/security-headers.conf

COPY frontend/ /usr/share/nginx/html/

# Long-lived caching for fingerprinted assets only. index.html must never be
# cached, or a deploy leaves clients pinned to a stale shell.
RUN mkdir -p /var/cache/nginx /var/log/nginx /tmp \
    && chown -R nginx:nginx /var/cache/nginx /var/log/nginx /tmp

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=4s --start-period=5s --retries=3 \
    CMD wget -q -O /dev/null http://127.0.0.1/healthz || exit 1

CMD ["nginx", "-g", "daemon off;"]
