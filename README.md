---
title: OfficeFlow Backend
emoji: 🏢
colorFrom: blue
colorTo: purple
sdk: docker
pinned: false
license: mit
---

# OfficeFlow AI Attendance Backend

Security rollout and verification status: [SECURITY_ROLLOUT.md](SECURITY_ROLLOUT.md).
The new authentication contract rejects legacy tokens and requires employee invitations.

FastAPI backend for OfficeFlow attendance app with face recognition, geofencing, and smart attendance features.

## Features
- 🔐 JWT Authentication
- 👤 Face Recognition (1:N search with DeepFace)
- 📍 Geofencing validation
- 📶 WiFi signal strength verification
- 🔒 Device binding security
- 📊 Attendance analytics

## API Endpoints
- `GET /` - Health check
- `POST /register` - User registration
- `POST /login` - User login
- `POST /smart-attendance` - Smart check-in/check-out
- `GET /logs/{email}` - Attendance history
- `GET /analytics/{email}` - Work hour analytics

## Tech Stack
- FastAPI
- MongoDB Atlas
- DeepFace (face recognition)
## Deployment

To deploy the backend on a VPS, use the following commands or run the `deploy_vps.sh` script:

```bash
cd /var/www/backend
git pull origin main
docker stop logday-api && docker rm logday-api
docker build -t logday-backend .
docker run -d \
  --name logday-api \
  --restart always \
  -p 8001:8001 \
  --env-file .env \
  -v $(pwd)/uploads:/app/uploads \
  -v $(pwd)/logs:/app/logs \
  logday-backend
```

Alternatively, run:
```bash
bash deploy_vps.sh
```

