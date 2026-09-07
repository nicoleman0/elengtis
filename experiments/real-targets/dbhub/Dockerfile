FROM node:22-slim@sha256:83f487e0a63425e5b4d146fb5e5be574bcbe1b7b843d3ebafdd95eaf7767a7e5

WORKDIR /opt/dbhub
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts --no-audit && npm cache clean --force
COPY dbhub.toml seed.sql entrypoint.mjs ./
RUN chown -R 10001:10001 /opt/dbhub
ENV NODE_NO_WARNINGS=1
USER 10001:10001
EXPOSE 8080
ENTRYPOINT ["node", "/opt/dbhub/entrypoint.mjs"]
