FROM node:22-slim@sha256:83f487e0a63425e5b4d146fb5e5be574bcbe1b7b843d3ebafdd95eaf7767a7e5

WORKDIR /opt/everything
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts --no-audit \
    && npm cache clean --force

ENV PORT=3001
USER 10001:10001
EXPOSE 3001
ENTRYPOINT ["./node_modules/.bin/mcp-server-everything", "streamableHttp"]
