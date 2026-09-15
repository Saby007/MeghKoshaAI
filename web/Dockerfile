ARG NODE_IMAGE=node:24-alpine@sha256:50c8e8ca1d27439048670df5883f32d57cf81cff6233222c893fd0d9884cbd81
ARG NGINX_IMAGE=nginxinc/nginx-unprivileged:stable-alpine@sha256:442753882674b49ae2c1de83ed67896131c0777f56df5005e356e62bc3f7e7ce
FROM ${NODE_IMAGE} AS build

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY index.html auth-callback.html tsconfig.json vite.config.ts ./
COPY public ./public
COPY src ./src
RUN npm run build

FROM ${NGINX_IMAGE} AS runtime
ENV NGINX_ENVSUBST_FILTER="^API_HOST$"
COPY --from=build --chown=101:101 /app/dist/ /usr/share/nginx/html/
COPY --chown=101:101 nginx/default.conf.template /etc/nginx/templates/default.conf.template
USER 101:101
EXPOSE 8080
CMD ["nginx", "-g", "daemon off;"]