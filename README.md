# 86'd API

Bar inventory management API for iOS app. Helps bartenders scan bottles and track inventory with offline-first sync.

## Tech Stack

- **Framework:** FastAPI (Python 3.11+)
- **Database:** SQLite with WAL mode
- **Auth:** JWT tokens
- **Deploy:** Render free tier

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Run locally
uvicorn main:app --reload

# API docs available at http://localhost:8000/docs
```

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `DATABASE_PATH` | SQLite database file path | `86d.db` |
| `SECRET_KEY` | JWT signing key | (change in production) |

## API Endpoints

### Health & Info
- `GET /` - API info
- `GET /health` - Health check

### Auth
- `POST /auth/register` - Create account
- `POST /auth/login` - Login
- `POST /auth/refresh` - Refresh token

### Products
- `GET /products` - List products
- `GET /products/search?q={query}` - Search
- `GET /products/barcode/{upc}` - Lookup by UPC
- `POST /products` - Add product (auth)
- `POST /products/{id}/increment-scan` - Increment scan count

### Locations
- `GET /locations` - List locations
- `POST /locations` - Create location
- `GET /locations/{id}/par-levels` - Get par levels
- `POST /locations/{id}/par-levels` - Set par level
- `POST /locations/{id}/par-levels/bulk` - Bulk update

### Inventory
- `POST /inventory/start` - Start session
- `GET /inventory/{id}` - Get session
- `POST /inventory/{id}/scan` - Add scan
- `POST /inventory/{id}/scan/bulk` - Bulk scans
- `POST /inventory/{id}/voice` - Add voice note
- `POST /inventory/{id}/complete` - Complete & generate order
- `POST /inventory/{id}/cancel` - Cancel session

### Orders
- `GET /orders` - List orders
- `GET /orders/{id}` - Get order
- `POST /orders/{id}/export` - Export order

### Sync
- `POST /sync` - Bulk sync (offline support)
- `GET /sync/{location_id}` - Get location data

## Database Schema

See `database.py` for full schema. Key tables:

- `users` - User accounts
- `locations` - Bars/venues
- `products` - Master product database (25 seeded)
- `par_levels` - Target stock levels
- `inventory_sessions` - Count sessions
- `scans` - Individual bottle scans
- `voice_notes` - Voice recordings
- `orders` - Generated orders

## Deployment

### Render

1. Create new Web Service
2. Connect GitHub repo `m700devops/86d-api`
3. Set build command: `pip install -r requirements.txt`
4. Set start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
5. Deploy

## License

MIT