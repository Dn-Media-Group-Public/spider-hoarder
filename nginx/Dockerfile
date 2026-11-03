# nginx with its config baked in (no host mounts, so it can run on any node). deploy.sh assembles the context in .build/nginx:
# this file, nginx.conf.template, and the generated aliases.map and landing.html.
FROM nginx:1.27
COPY nginx.conf.template /etc/nginx/templates/default.conf.template
COPY aliases.map /etc/nginx/aliases.map
COPY landing.html /usr/share/nginx/html/index.html
