# Plataforma Madre — API

Backend real (probado, no solo diseñado) de la plataforma interna de Solutions:
clientes (persona/empresa), servicios de 4 líneas de negocio (Taxes, Contabilidad,
Capital, Otros Servicios), trabajadores con sus tipos de servicio configurados,
comisiones y Nómina, temporadas y estadísticas — con permisos binarios
**Admin** (ve y modifica todo) / **Trabajador** (solo Clientes y sus propios
tipos de servicio).

Es la contraparte real del [prototipo interactivo](../plataforma-madre-prototipo.html)
y del [documento de estrategia](../contabilidad-estrategia.html) — mismo modelo de
datos, mismas reglas de negocio, ahora con una base de datos de verdad detrás.

---

## Qué ya funciona (probado con `curl` en esta sesión)

- Login por sesión (modo desarrollo sin Google todavía — ver "Cuentas que faltan" abajo)
- Buscar/listar clientes, ver perfil, crear cliente nuevo
- SSN/ITIN completo cifrado en reposo (Fernet); el perfil solo expone los últimos 4;
  un endpoint aparte revela el completo y lo deja en log
- Crear/editar servicio con el modelo núcleo fijo + `detalle` (JSON) por línea
- Un trabajador solo puede crear servicios de los tipos que tiene configurados —
  probado que un tipo no configurado da 403
- El trabajador asignado a un servicio se toma de la sesión, no se puede falsear
  (salvo Admin, que sí puede asignar a nombre de otro)
- Nómina: pendientes por trabajador, marcar como pagado (mueve las comisiones a
  `pagada` y las saca de "pendientes"), historial de pagos
- Estadísticas por periodo (temporada/post-temporada automático, o temporadas
  creadas a mano)
- Endpoints de Trabajadores/Nómina/Estadísticas devuelven 403 si quien llama no
  es Admin — probado

## Qué falta (siguiente fase, no bloquea empezar)

- **Login real con Google** — el código de OAuth está escrito (`backend/auth.py`,
  usando `authlib`) pero no se puede probar sin las credenciales reales de Google
  Cloud (ver abajo). Mientras tanto, `AUTH_DEV_MODE=true` deja loguearse solo con
  el correo, para desarrollar sin depender de eso.
- **Integración con Google Drive** (archivos/fotos) — hoy `Archivo` solo guarda
  nombre y metadatos; falta conectar la subida real a Drive vía la cuenta de
  servicio del Workspace.
- **Frontend real** — el prototipo interactivo (HTML/JS con datos en memoria) es
  la referencia visual; falta conectarlo a esta API (cambiar el `state` en
  memoria por `fetch()` a estos endpoints es, en su mayoría, mecánico).
- **Import del histórico** (2023–2025 y los 9 forms de preparador) — falta el
  script de importación; el modelo ya está listo para recibirlo (sección 04 del
  documento: núcleo fijo + `detalle` flexible es justo para esto).
- **Log de acceso a SSN revelado** — hoy se imprime a consola (`print(...)` en
  `revelar_ssn`); en producción debe ser una tabla real con trabajador, fecha y
  cliente consultado.

---

## Cómo correrlo localmente

```bash
cd plataforma-madre
cp .env.example .env
# Genera y pega en .env:
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# Para desarrollar sin credenciales de Google todavía, agrega también:
echo "AUTH_DEV_MODE=true" >> .env

./run.sh   # crea venv, instala deps, siembra datos de ejemplo, arranca
```

Abre **http://localhost:8000/docs** — ahí están todos los endpoints, probables
directo desde el navegador (Swagger).

Para "loguearte" en modo desarrollo:
```bash
curl -c cookies.txt -X POST http://localhost:8000/auth/dev-login \
  -H "Content-Type: application/json" \
  -d '{"correo":"tu@solutionsmultiservices.com"}'   # admin, ve todo

curl -b cookies.txt http://localhost:8000/api/clientes
```

## Estructura

```
plataforma-madre/
├── backend/
│   ├── app.py         # rutas de la API (clientes, servicios, trabajadores, nómina, estadísticas)
│   ├── models.py       # tablas SQLAlchemy — el modelo de datos completo
│   ├── database.py      # conexión + cifrado/descifrado de SSN
│   ├── auth.py           # login Google OAuth (real) + modo desarrollo
│   ├── catalogo.py         # servicio_tipo y sus campos de detalle por línea
│   └── seed.py              # datos de ejemplo (los mismos del prototipo)
├── requirements.txt
├── run.sh
└── .env.example
```

---

## Cuentas que faltan para producción (esto no lo puedo crear yo)

Por política de seguridad no creo cuentas ni entro credenciales de terceros —
esto lo tiene que hacer alguien del equipo con acceso admin.

### 1. Proyecto en Google Cloud Console (para el login) — gratis

1. Entra a [console.cloud.google.com](https://console.cloud.google.com) con una
   cuenta del Workspace de Solutions.
2. Crea un proyecto nuevo (ej. "Plataforma Madre").
3. En **APIs y servicios → Pantalla de consentimiento OAuth**: tipo "Interno"
   (restringe el login a su propio dominio automáticamente — no hace falta
   configurar el dominio permitido a mano).
4. En **Credenciales → Crear credenciales → ID de cliente de OAuth**, tipo
   "Aplicación web". En "URI de redirección autorizados" agrega la URL real
   una vez que esté desplegada (ej. `https://plataforma.solutionsmultiservices.com/auth/callback`).
5. Copia el **Client ID** y **Client Secret** a `GOOGLE_CLIENT_ID` /
   `GOOGLE_CLIENT_SECRET` en el `.env` de producción.

No tiene costo — Google Cloud no cobra por usar OAuth para login.

### 2. Cuenta de Render (hosting) — con costo mensual

1. Crea la cuenta en [render.com](https://render.com) con el correo del equipo
   que ya usan para el extractor (así queda todo en un solo lugar).
2. Dos servicios a crear ahí: un **Web Service** (para este backend — y luego
   el frontend) y una base de datos **PostgreSQL**.
3. Costo aproximado (confirmar en render.com/pricing, cambia con el tiempo):
   Web Service desde ~$7 USD/mes, Postgres administrado desde ~$6–19 USD/mes
   según el tamaño. Para el tamaño de esta operación (unos miles de servicios
   al año), el nivel más económico de cada uno alcanza para empezar.
4. Al desplegar, Render te da la `DATABASE_URL` de Postgres lista para pegar
   en las variables de entorno del Web Service — no hay que instalar ni
   configurar Postgres a mano.

**Mientras no tengan las dos cuentas:** el código ya corre completo en su
laptop con SQLite (como se prueba arriba) — no bloquea seguir desarrollando el
resto (frontend real, importación del histórico, Drive) en paralelo.
