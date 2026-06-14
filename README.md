# MaryJaneTimeLapse

Aplicación web Dockerizada para crear timelapses desde cámaras IP con stream RTSP.

## Características

- **Captura automática** de frames desde stream RTSP (intervalo configurable)
- **Dashboard web** con visualización de frames agrupados por día
- **Generación de timelapse** por rango de fechas
- **Interfaz responsive** sin dependencias externas
- **100% Docker** - sin instalar nada en el host

## Requisitos

- Docker & Docker Compose
- Cámara IP con stream RTSP accesible

## Uso Rápido

```bash
git clone <repo-url>
cd MaryJaneTimeLapse

# Configurar variables en docker-compose.yml
# - CAMERA_URL: URL RTSP de tu cámara
# - CAPTURE_INTERVAL: segundos entre capturas

# Iniciar
docker compose up -d

# Acceder a la app
# http://localhost:5000
```

## Configuración

Editar `docker-compose.yml`:

```yaml
environment:
  - CAMERA_URL=rtsp://admin:password@192.168.0.63:554/cam/realmonitor?channel=1&subtype=0
  - CAPTURE_INTERVAL=60
```

## Volúmenes

- `./frames:/app/frames` - Frames capturados
- `./videos:/app/videos` - Videos timelapse generados

## API Endpoints

| Endpoint | Método | Descripción |
|----------|--------|-------------|
| `/` | GET | Dashboard |
| `/api/capture` | POST | Capturar frame ahora |
| `/api/timelapse` | POST | Generar timelapse (día desde/hasta) |
| `/api/frames` | GET | Listar frames |
| `/api/videos` | GET | Listar videos |
| `/api/delete_frames` | POST | Borrar todos los frames |
| `/api/config` | POST | Cambiar intervalo |

## Licencia

MIT
