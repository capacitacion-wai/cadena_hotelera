# Gestor de ocupación — Referencia de mantenimiento

## Arquitectura

Aplicación modular con React 19, TypeScript, Vite y MUI en el cliente; FastAPI y SQLAlchemy asíncrono en el servicio. El almacén es SQLite local en el volumen persistente de la aplicación. No se consulta ni se modifica el PostgreSQL del proyecto `demo_wai`; `PROJECT_ID=43` solo se usa para validar el acceso WAI.

Los modelos y servicios de ocupación viven en `src/backend/app/modules/ocupacion/`; propuestas, aprobaciones e historial, en `src/backend/app/modules/reubicaciones/`. `app_database.py` crea un motor SQLite separado, WAL, claves foráneas y un único escritor. Cada operación de escritura confirma dentro del servicio antes de responder. Los endpoints usan `app.wai.perfilado.requiere` para aplicar el perfil autorizado; la carga de datos de ejemplo requiere `CAP-4/crear` y solo continúa si no existe ningún hotel, y la aprobación requiere además el perfil `direccion`, incluso para quien tenga administración técnica.

El módulo de perfilado se instala y actualiza con `assets/modulos.py`; sus archivos son invariantes y no se editan a mano. Los datos de muestra y paquetes se definen en `datos_de_ejemplo.py`. `pruebas_de_serie.py` y `pruebas_router.py` son assets comunes; no se modifican. Las pruebas del usuario en `tests-e2e/` las ejecuta únicamente la plataforma.

## Operaciones

- `GET /api/ocupacion`: totales de la cadena y sobreventa.
- `GET /api/hoteles`: hoteles paginados y ocupación calculada.
- `GET /api/hoteles/{hotel_id}`: resumen de un hotel.
- `GET /api/hoteles/{hotel_id}/habitaciones`: habitaciones paginadas.
- `GET /api/hoteles/{hotel_id}/reservas`: reservas activas de esta noche, paginadas.
- `POST /api/ocupacion/datos-de-ejemplo`: carga una sola vez los datos de los cinco hoteles, solo con el permiso de Dirección y si aún no hay hoteles; devuelve `409` si ya existen.
- `GET /api/reubicaciones`: propuestas pendientes, historial aprobado, huéspedes sin asignación y destinos con espacio; cada lista se pagina.
- `POST /api/reubicaciones`: crea una propuesta con motivo; no mueve la reserva.
- `POST /api/reubicaciones/{propuesta_id}/aprobar`: comprueba el perfil Dirección, asigna una habitación libre, mueve la reserva y registra la aprobación.
- `POST /api/auth/login` y `POST /api/auth/handoff`: rutas base de autenticación WAI.
- `GET /api/health`: comprobación de disponibilidad, sin autenticación.

## Configuración y almacenamiento

El despliegue proporciona `WAI_BACKEND_URL`, `WAI_INTROSPECCION_URL`, `PROJECT_ID`, `DB_SCHEMA`, `APP_NAME`, `APP_DB_PATH` y `APP_DATABASE_URL`. `APP_DB_PATH` apunta a `/app-data/app.sqlite`; no se incluyen contraseñas ni claves en el código. Las variables de acceso al PostgreSQL del proyecto no se consumen porque esta aplicación guarda sus datos operativos en SQLite.

## Comprobación local

`src/backend/requirements.txt` incluye FastAPI, Uvicorn, SQLAlchemy, `aiosqlite`, `python-jose` y `httpx`; `requirements-test.txt` incluye pytest y pytest-asyncio. Las pruebas unitarias viven en `src/backend/tests/`. La interfaz se comprueba con `npm run build` y `npm test` desde `src/frontend/`. Las pruebas de aceptación declaradas en la Definición viven en `tests-e2e/` y no se ejecutan con pytest local: se lanzan y se leen mediante las herramientas de pruebas de WAI.

## Empaquetado y publicación

`Dockerfile` construye el cliente y lo copia al servicio para servir las rutas del navegador. `k8s/` contiene descriptores de referencia; el publicador de WAI crea los recursos efectivos a partir del manifiesto y la configuración del tenant. La aplicación utiliza el servicio WAI para iniciar sesión e introspeccionar la sesión, y nunca guarda una clave de firma.

El almacenamiento local está limitado a una réplica. Las altas, modificaciones y bajas deben seguir pasando por `app_write()` y quedar asociadas a su operación de negocio. No se deben añadir escrituras al proyecto `demo_wai`. Si se cambia una ruta, un permiso o la forma de las respuestas, hay que revisar la Definición y los casos de prueba antes de cambiar el código.
