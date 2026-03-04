import uuid
from datetime import datetime, timezone
from typing import Optional

def generate_id() -> str:
    """Generate UUID v4 string"""
    return str(uuid.uuid4())

def now_iso() -> str:
    """Current time in ISO 8601 format (UTC)"""
    return datetime.now(timezone.utc).isoformat()

def level_to_decimal(level: str) -> float:
    """Convert level string to decimal value"""
    mapping = {
        "full": 1.0,
        "almost_full": 1.0,  # Backwards compatibility
        "3/4": 0.75,
        "half": 0.5,
        "1/4": 0.25,
        "empty": 0.0,
    }
    return mapping.get(level.lower(), 0.5)

def decimal_to_level(decimal: float) -> str:
    """Convert decimal to level string"""
    if decimal >= 0.875:
        return "almost_full"
    elif decimal >= 0.625:
        return "3/4"
    elif decimal >= 0.375:
        return "half"
    elif decimal >= 0.125:
        return "1/4"
    else:
        return "empty"

def calculate_variance(
    current_usage: float,
    history: list[float]
) -> Optional[dict]:
    """
    Calculate variance alert if usage is unusual.
    Returns alert dict or None.
    
    Rules:
    - Alert if usage > avg * 2 (high usage)
    - Alert if usage < avg * 0.5 AND avg > 1 (low usage)
    """
    if not history:
        return None
    
    avg_usage = sum(history) / len(history)
    
    # Avoid division by zero
    if avg_usage == 0:
        if current_usage > 2:
            return {
                "type": "high",
                "current": current_usage,
                "average": avg_usage,
                "variance_percent": 100,
                "message": f"Used {current_usage} bottles (usually 0)"
            }
        return None
    
    variance_percent = ((current_usage - avg_usage) / avg_usage) * 100
    
    if current_usage > avg_usage * 2:
        return {
            "type": "high",
            "current": current_usage,
            "average": round(avg_usage, 1),
            "variance_percent": round(variance_percent, 0),
            "message": f"Used {current_usage} bottles vs avg {avg_usage:.1f} — possible theft or waste"
        }
    
    if current_usage < avg_usage * 0.5 and avg_usage > 1:
        return {
            "type": "low",
            "current": current_usage,
            "average": round(avg_usage, 1),
            "variance_percent": round(variance_percent, 0),
            "message": f"Used {current_usage} bottles vs avg {avg_usage:.1f} — slow week or counting error?"
        }
    
    return None

def generate_order_items(
    scans: list[dict],
    par_levels: dict[str, float]  # product_id -> par quantity
) -> list[dict]:
    """
    Generate order items by comparing current inventory to par levels.
    
    Returns list of items to order.
    """
    # Aggregate scans by product
    inventory = {}  # product_id -> total amount
    
    for scan in scans:
        product_id = scan["product_id"]
        # Level decimal + quantity (for backup bottles)
        amount = scan["level_decimal"] + (scan.get("quantity", 1) - 1)
        
        if product_id in inventory:
            inventory[product_id] += amount
        else:
            inventory[product_id] = amount
    
    # Calculate order quantities
    order_items = []
    
    for product_id, par in par_levels.items():
        current = inventory.get(product_id, 0)
        order_qty = max(0, par - current)
        
        if order_qty > 0:
            # Determine urgency
            if current == 0:
                urgency = "critical"
            elif order_qty >= par * 0.5:
                urgency = "moderate"
            else:
                urgency = "normal"
            
            order_items.append({
                "product_id": product_id,
                "current_amount": round(current, 2),
                "par_level": par,
                "order_quantity": round(order_qty, 0),
                "urgency": urgency
            })
    
    # Sort by urgency (critical first)
    urgency_order = {"critical": 0, "moderate": 1, "normal": 2}
    order_items.sort(key=lambda x: urgency_order[x["urgency"]])
    
    return order_items