# 86'd API

Bar inventory management API for iOS app. Helps bartenders scan bottles and track inventory with offline-first sync.

**Live API:** https://eight6d-api.onrender.com

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

All endpoints are prefixed with `/v1/`

### Health & Info
- `GET /` - API info
- `GET /health` - Health check

### Auth
- `POST /v1/auth/register` - Create account
- `POST /v1/auth/login` - Login
- `POST /v1/auth/refresh` - Refresh token
- `POST /v1/auth/forgot-password` - Request password reset
- `POST /v1/auth/reset-password` - Reset password with token
- `PUT /v1/auth/change-password` - Change password (auth required)

### Users
- `GET /v1/users/me` - Get user profile
- `DELETE /v1/users/me` - Delete account
- `POST /v1/users/me/accept-terms` - Accept terms & privacy

### Products
- `GET /v1/products` - List products
- `GET /v1/products/search?q={query}` - Search
- `GET /v1/products/barcode/{upc}` - Lookup by UPC
- `POST /v1/products` - Add product (auth)
- `POST /v1/products/{id}/increment-scan` - Increment scan count

### Locations
- `GET /v1/locations` - List locations
- `POST /v1/locations` - Create location
- `GET /v1/locations/{id}/par-levels` - Get par levels
- `POST /v1/locations/{id}/par-levels` - Set par level
- `POST /v1/locations/{id}/par-levels/bulk` - Bulk update

### Distributors
- `GET /v1/distributors` - List distributors
- `POST /v1/distributors` - Create distributor
- `PUT /v1/distributors/{id}` - Update distributor
- `DELETE /v1/distributors/{id}` - Delete distributor
- `GET /v1/locations/{id}/product-distributors` - List product-distributor assignments
- `POST /v1/locations/{id}/product-distributors` - Assign product to distributor

### Inventory
- `POST /v1/inventory/start` - Start session
- `GET /v1/inventory/{id}` - Get session
- `POST /v1/inventory/{id}/scan` - Add scan
- `POST /v1/inventory/{id}/scan/bulk` - Bulk scans
- `POST /v1/inventory/{id}/voice` - Add voice note
- `POST /v1/inventory/{id}/complete` - Complete & generate order
- `POST /v1/inventory/{id}/cancel` - Cancel session

### Orders
- `GET /v1/orders` - List orders
- `GET /v1/orders/{id}` - Get order
- `POST /v1/orders/{id}/export` - Export order
- `POST /v1/orders/{id}/prepare-emails` - Prepare distributor emails

### Sync
- `POST /v1/sync` - Bulk sync (offline support)
- `GET /v1/sync/{location_id}` - Get location data

## Database Schema

See `database.py` for full schema. Key tables:

- `users` - User accounts (with subscription fields)
- `locations` - Bars/venues
- `products` - Master product database (25 seeded)
- `distributors` - Distributor contacts
- `location_product_distributors` - Product-distributor mappings
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

**URL:** https://eight6d-api.onrender.com

## License

MIT
