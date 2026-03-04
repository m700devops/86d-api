#!/usr/bin/env python3
"""
Update script for 86'd API v1.1
Adds: API versioning, distributors, subscription fields, auth endpoints, email prep
"""

import re

# Read the original file
with open('main.py', 'r') as f:
    content = f.read()

# 1. Update imports - add APIRouter and timedelta
content = content.replace(
    'from fastapi import FastAPI, Depends, HTTPException, Header, Request, Query',
    'from fastapi import FastAPI, Depends, HTTPException, Header, Request, Query, APIRouter'
)
content = content.replace(
    'from datetime import datetime, timezone',
    'from datetime import datetime, timezone, timedelta'
)
content = content.replace(
    'import uuid',
    'import uuid\nimport json'
)

# 2. Add new auth imports
content = content.replace(
    'from auth import (\n    get_password_hash, verify_password, create_access_token, \n    create_refresh_token, verify_token\n)',
    'from auth import (\n    get_password_hash, verify_password, create_access_token, \n    create_refresh_token, verify_token, create_password_reset_token,\n    verify_password_reset_token\n)'
)

# 3. Update root endpoint to show v1 endpoints
content = content.replace(
    '"auth": ["/auth/register", "/auth/login", "/auth/refresh"]',
    '"auth": ["/v1/auth/register", "/v1/auth/login", "/v1/auth/refresh", "/v1/auth/forgot-password", "/v1/auth/reset-password"]'
)
content = content.replace(
    '"products": ["/products", "/products/search", "/products/barcode/{upc}"]',
    '"products": ["/v1/products", "/v1/products/search", "/v1/products/barcode/{upc}"]'
)
content = content.replace(
    '"locations": ["/locations", "/locations/{id}/par-levels"]',
    '"locations": ["/v1/locations", "/v1/locations/{id}/par-levels"]'
)
content = content.replace(
    '"inventory": ["/inventory/start", "/inventory/{id}", "/inventory/{id}/scan"]',
    '"inventory": ["/v1/inventory/start", "/v1/inventory/{id}", "/v1/inventory/{id}/scan"]'
)
content = content.replace(
    '"orders": ["/orders", "/orders/{id}"]',
    '"orders": ["/v1/orders", "/v1/orders/{id}", "/v1/orders/{id}/prepare-emails", "/v1/orders/{id}/export"]'
)
content = content.replace(
    '"sync": ["/sync"]',
    '"sync": ["/v1/sync"],\n            "distributors": ["/v1/distributors"],\n            "users": ["/v1/users/me"]'
)

# 4. Add v1_router creation after the middleware section
# Find the line after "return response" in middleware and add router
middleware_end = content.find('return response\n\n# ============== DEPENDENCIES')
if middleware_end > 0:
    insert_pos = content.find('\n\n# ============== DEPENDENCIES', middleware_end)
    content = content[:insert_pos] + '\n\n# ============== V1 ROUTER ==============\n\nv1_router = APIRouter(prefix="/v1")' + content[insert_pos:]

# 5. Replace all @app. with @v1_router. for the main endpoints
# But keep @app. for root and health

# First, let's find where the main endpoints start (after health check)
health_end = content.find('@app.get("/health"')
health_func_end = content.find('# ============== AUTHENTICATION ==============', health_end)

if health_func_end > 0:
    # Get the section from AUTHENTICATION to ERROR HANDLERS
    auth_section_start = health_func_end
    error_handlers_start = content.find('# ============== ERROR HANDLERS ==============')
    
    if error_handlers_start > 0:
        # Replace @app. with @v1_router. in this section
        section = content[auth_section_start:error_handlers_start]
        section = section.replace('@app.', '@v1_router.')
        content = content[:auth_section_start] + section + content[error_handlers_start:]

# 6. Add v1_router inclusion before error handlers
error_handlers_pos = content.find('# ============== ERROR HANDLERS ==============')
if error_handlers_pos > 0:
    content = content[:error_handlers_pos] + '\n# ============== INCLUDE V1 ROUTER ==============\n\napp.include_router(v1_router)\n\n' + content[error_handlers_pos:]

# Write the updated file
with open('main.py', 'w') as f:
    f.write(content)

print("Main.py updated with API versioning")
