# ──────────────────────────────────────────────────────────────────────────
# prisma-front para DESARROLLO LOCAL (docker compose). No se usa en CI ni en ECS.
#
# Por qué existe en vez de usar prisma-front/Dockerfile:
#   el Dockerfile del repo pisa `.env.production` con solo tres variables vacías
#   (VITE_BFF_URL, VITE_CHAT_API_URL, VITE_DOCS_API_URL), asi que el bundle queda SIN las
#   VITE_COGNITO_* y el front cae al login legacy. Solo funcionaba porque `COPY . .`
#   arrastraba el `.env` del disco del desarrollador.
#   Aqui los datos de Cognito entran por build-arg, asi el pool se cambia desde el `.env`
#   de la raiz sin tocar el repo.
#
# Las URLs de la API quedan VACIAS a proposito: el navegador llama same-origin y nginx
# reparte (/api -> bff, /chat -> workflow). Es el mismo camino que en produccion.
# ──────────────────────────────────────────────────────────────────────────

FROM node:20-alpine AS builder

WORKDIR /app

COPY package*.json ./
# npm install (no npm ci): el lock se versiona desde Windows y aqui se compila en Linux,
# por lo que las optionalDependencies nativas de Linux no estan en el lock.
RUN npm install --no-audit --no-fund

COPY . .

# Los tres valores de Cognito NO son secretos (Vite los hornea en un bundle publico).
ARG VITE_COGNITO_USER_POOL_ID
ARG VITE_COGNITO_CLIENT_ID
ARG VITE_COGNITO_DOMAIN

RUN printf 'VITE_BFF_URL=\nVITE_CHAT_API_URL=\nVITE_DOCS_API_URL=\nVITE_COGNITO_USER_POOL_ID=%s\nVITE_COGNITO_CLIENT_ID=%s\nVITE_COGNITO_DOMAIN=%s\n' \
    "$VITE_COGNITO_USER_POOL_ID" "$VITE_COGNITO_CLIENT_ID" "$VITE_COGNITO_DOMAIN" > .env.production

RUN npm run build

FROM nginx:1.27-alpine AS runtime

COPY nginx.conf.template /etc/nginx/templates/default.conf.template

# 127.0.0.11 es el DNS embebido de Docker. Los upstreams son nombres de servicio del compose.
ENV NGINX_RESOLVER=127.0.0.11 \
    BFF_UPSTREAM=bff:3010 \
    WORKFLOW_UPSTREAM=workflow:8000

COPY --from=builder /app/dist /usr/share/nginx/html

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
