-- Una base de datos por servicio, dentro de la misma instancia de Postgres.
--
-- En produccion todos comparten UNA base con esquemas separados (users, jobs, public).
-- En local las separamos porque cada servicio lleva su propia tabla `_prisma_migrations`
-- y, compartiendo base, `prisma migrate deploy` de un servicio vería como "desconocidas"
-- las migraciones de los otros.
--
-- Este script solo corre la PRIMERA vez que se crea el volumen `pgdata`.
-- Si agregas una base aqui, recrea el volumen: `docker compose down -v`.

CREATE DATABASE prisma_users;
CREATE DATABASE prisma_docs;
CREATE DATABASE prisma_perfil;
CREATE DATABASE prisma_adminpanel;
CREATE DATABASE prisma_monitor;   -- respaldo (tabla log) del monitor en vivo
