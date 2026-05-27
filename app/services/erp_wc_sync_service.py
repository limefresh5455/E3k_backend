import requests
import time
import logging
from requests.auth import HTTPBasicAuth
from dotenv import load_dotenv
import os

load_dotenv()

logger = logging.getLogger("sync_engine")

# ================== CONFIGURATION ==================
ERP_URL      = os.getenv("ERP_URL")
ERP_USERNAME = os.getenv("ERP_USERNAME")
ERP_PASSWORD = os.getenv("ERP_PASSWORD")

WC_BASE_URL        = os.getenv("WOOCOMMERCE_BASE_URL", "").rstrip("/")
WC_CONSUMER_KEY    = os.getenv("WOOCOMMERCE_CONSUMER_KEY", "")
WC_CONSUMER_SECRET = os.getenv("WOOCOMMERCE_CONSUMER_SECRET", "")

# Retry / throttle config
MAX_RETRIES            = 2
RETRY_DELAY            = 10
DELAY_BETWEEN_REQUESTS = 0.5   # seconds between every WC request
DELAY_AFTER_429        = 30    # base wait after a 429 response
MAX_429_RETRIES        = 5
# ===================================================

wc_auth = HTTPBasicAuth(WC_CONSUMER_KEY, WC_CONSUMER_SECRET)

WC_AUTH_PARAMS = {
    "consumer_key": WC_CONSUMER_KEY,
    "consumer_secret": WC_CONSUMER_SECRET,
}

# ─────────────────────────────────────────────────
# SKU HELPERS
# ─────────────────────────────────────────────────
def normalize(sku: str) -> str:
    """Strip dots, stars, dashes, spaces → uppercase digits-only key."""
    return (
        str(sku)
        .strip()
        .lstrip("*")
        .strip()
        .replace(".", "")
        .replace("-", "")
        .replace(" ", "")
        .upper()
    )


def clean(sku: str) -> str:
    return str(sku).strip().lstrip("*").strip()


def is_backorder_sku(sku: str) -> bool:
    """
    Returns True if the article number matches the decimal format xx.xxxx
    e.g. '48.2345' — two digits, a dot, then four digits.
    These get onbackorder (qty=0) instead of outofstock.
    """
    import re
    return bool(re.match(r'^\d{2}\.\d+$', str(sku).strip()))


# ─────────────────────────────────────────────────
# WC GET with 429-aware retry + per-request delay
# ─────────────────────────────────────────────────
def wc_get_with_retry(url: str, params: dict | None = None) -> requests.Response:
    if params is None:
        params = {}

    merged_params = {**WC_AUTH_PARAMS, **params}

    for attempt in range(MAX_429_RETRIES):
        time.sleep(DELAY_BETWEEN_REQUESTS)
        try:
            resp = requests.get(url, auth=wc_auth, params=merged_params, timeout=60)

            if resp.status_code == 429:
                wait = DELAY_AFTER_429 * (attempt + 1)
                logger.warning("429 Too Many Requests — waiting %ds (attempt %d/%d)",
                               wait, attempt + 1, MAX_429_RETRIES)
                time.sleep(wait)
                continue

            resp.raise_for_status()
            return resp

        except requests.exceptions.RequestException as exc:
            if attempt < MAX_429_RETRIES - 1:
                logger.warning("Request error: %s — retrying in %ds…", exc, DELAY_AFTER_429)
                time.sleep(DELAY_AFTER_429)
            else:
                raise

    raise RuntimeError(f"Failed after {MAX_429_RETRIES} retries for URL: {url}")


# ─────────────────────────────────────────────────
# WC PUT with 429-aware retry + per-request delay
# ─────────────────────────────────────────────────
def wc_put_with_retry(url: str, payload: dict, params: dict | None = None) -> requests.Response:
    if params is None:
        params = {}
        
    merged_params = {**WC_AUTH_PARAMS, **params}
    for attempt in range(MAX_429_RETRIES):
        time.sleep(DELAY_BETWEEN_REQUESTS)
        try:
            resp = requests.put(
                url,
                auth=wc_auth,
                params=merged_params,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=60,
            )

            if resp.status_code == 429:
                wait = DELAY_AFTER_429 * (attempt + 1)
                logger.warning("PUT 429 — waiting %ds (attempt %d/%d)",
                               wait, attempt + 1, MAX_429_RETRIES)
                time.sleep(wait)
                continue

            resp.raise_for_status()
            return resp

        except requests.exceptions.RequestException as exc:
            if attempt < MAX_429_RETRIES - 1:
                logger.warning("PUT error: %s — retrying in %ds…", exc, DELAY_AFTER_429)
                time.sleep(DELAY_AFTER_429)
            else:
                raise

    raise RuntimeError(f"PUT failed after {MAX_429_RETRIES} retries for URL: {url}")


# ─────────────────────────────────────────────────
# 1. FETCH ERP DATA
# ─────────────────────────────────────────────────
def fetch_erp_data() -> list[dict] | None:
    session = requests.Session()
    for attempt in range(MAX_RETRIES):
        try:
            logger.info("[ERP] Attempt %d/%d — fetching articles…", attempt + 1, MAX_RETRIES)
            response = session.get(
                ERP_URL,
                auth=(ERP_USERNAME, ERP_PASSWORD),
                headers={"accept": "application/json"},
                timeout=None,  # ERP is slow — no timeout
            )
            response.raise_for_status()
            data = response.json()
            logger.info("[ERP] Fetched %d records.", len(data))
            return data
        except requests.exceptions.RequestException as exc:
            logger.error("[ERP] Error on attempt %d: %s", attempt + 1, exc)
            if attempt < MAX_RETRIES - 1:
                logger.info("Retrying in %ds…", RETRY_DELAY)
                time.sleep(RETRY_DELAY)

    logger.error("[ERP] All retry attempts failed.")
    return None


# ─────────────────────────────────────────────────
# 2. BUILD ERP LOOKUP {normalized_sku: erp_item}
# ─────────────────────────────────────────────────
def build_erp_lookup(erp_data: list[dict]) -> dict:
    lookup: dict[str, dict] = {}
    for item in erp_data:
        raw = str(item.get("ArticleNumber", "")).strip()
        if not raw:
            continue
        key = normalize(raw)
        if key:
            lookup[key] = item
    logger.info("[ERP] Lookup built: %d normalized keys.", len(lookup))
    return lookup


# ─────────────────────────────────────────────────
# 3. FETCH ALL WC PRODUCTS (paginated)
# ─────────────────────────────────────────────────
def fetch_all_wc_products() -> list[dict]:
    logger.info("[WC] Fetching all products…")
    products: list[dict] = []
    page = 1
    while True:
        resp = wc_get_with_retry(
            f"{WC_BASE_URL}/wp-json/wc/v3/products",
            params={"per_page": 100, "page": page, "status": "any"},
        )
        batch = resp.json()
        if not batch:
            break
        products.extend(batch)
        logger.info("  Page %d: %d products (total: %d)", page, len(batch), len(products))
        page += 1
    logger.info("[WC] Total products fetched: %d", len(products))
    return products


# ─────────────────────────────────────────────────
# 4. FETCH VARIATIONS (paginated) for one product
# ─────────────────────────────────────────────────
def fetch_variations(product_id: int) -> list[dict]:
    variations: list[dict] = []
    page = 1
    while True:
        resp = wc_get_with_retry(
            f"{WC_BASE_URL}/wp-json/wc/v3/products/{product_id}/variations",
            params={"per_page": 100, "page": page},
        )
        batch = resp.json()
        if not batch:
            break
        variations.extend(batch)
        page += 1
    return variations


# ─────────────────────────────────────────────────
# 5. BUILD SKU MAP from all products + variations
# ─────────────────────────────────────────────────
def build_sku_map(products: list[dict]) -> dict:
    logger.info("[WC] Building SKU map from products and variations…")
    sku_map: dict[str, list[dict]] = {}

    def add(sku_raw: str, entry: dict) -> None:
        for key in {str(sku_raw).strip(), clean(sku_raw), normalize(sku_raw)}:
            if key:
                sku_map.setdefault(key, []).append(entry)

    total = len(products)
    for idx, product in enumerate(products, start=1):
        pid   = product.get("id")
        pname = product.get("name", "")
        ptype = product.get("type", "simple")
        psku  = str(product.get("sku", "")).strip()

        logger.debug("  [%d/%d] '%s' (ID=%s, type=%s)", idx, total, pname, pid, ptype)

        if ptype == "variable":
            try:
                variations = fetch_variations(pid)
                logger.debug("    → %d variations", len(variations))
                for var in variations:
                    vid   = var.get("id")
                    vsku  = str(var.get("sku", "")).strip()
                    vattr = ", ".join(
                        f"{a['name']}: {a['option']}"
                        for a in var.get("attributes", [])
                    )
                    vprice = var.get("price") or var.get("regular_price", "")
                    if vsku:
                        add(vsku, {
                            "product_id":               pid,
                            "product_name":             pname,
                            "product_sku":              psku,
                            "variation_id":             vid,
                            "variation_sku":            vsku,
                            "variation_sku_normalized": normalize(vsku),
                            "variation_attributes":     vattr,
                            "wc_price":                 vprice,
                        })
            except Exception as exc:
                logger.error("    ❌ Skipping variations for product %s: %s", pid, exc)
        else:
            if psku:
                add(psku, {
                    "product_id":               pid,
                    "product_name":             pname,
                    "product_sku":              psku,
                    "variation_id":             None,
                    "variation_sku":            None,
                    "variation_sku_normalized": None,
                    "variation_attributes":     None,
                    "wc_price":                 product.get("price") or product.get("regular_price", ""),
                })

    logger.info("[WC] SKU map built: %d unique SKU keys.", len(sku_map))
    return sku_map


# ─────────────────────────────────────────────────
# 6. BUILD UPDATE PAYLOAD
# ─────────────────────────────────────────────────
def build_update_payload(
    erp_price,
    erp_quantity: int | None,
    erp_description: str | None = None,
    raw_sku: str = "",
) -> dict:
    """
    Builds the WooCommerce update payload.
    Stock status logic:
      - quantity > 0                          → 'instock'
      - quantity == 0 + SKU format xx.xxxx   → 'onbackorder' (backorders: notify)
      - quantity == 0 + all other SKUs       → 'outofstock'
    Lead-time meta is set only for the relevant status key.
    """
    if erp_quantity is not None:
        qty = int(round(float(erp_quantity)))
    else:
        qty = 0

    des = erp_description if erp_description else ""
    price_str = str(erp_price) if erp_price is not None else ""

    # ── In Stock ──────────────────────────────────
    if qty > 0:
        return {
            "regular_price":  price_str,
            "price":          price_str,
            "manage_stock":   True,
            "stock_quantity": qty,
            "stock_status":   "instock",
            "backorders":     "no",
            "meta_data": [
                {
                    "key":   "_wclt_variation_lead_time",
                    "value": f"{qty} {des} auf Lager",
                },
                {
                    "key":   "_wclt_lead_time_instock",
                    "value": f"{qty} {des} auf Lager",
                },
            ],
        }

    # ── Out of Stock: decimal SKU (xx.xxxx) → Backorder ──
    if is_backorder_sku(raw_sku):
        backorder_msg = "Innerhalb von 3-4 Tagen lieferbar"
        return {
            "regular_price":  price_str,
            "price":          price_str,
            "manage_stock":   True,
            "stock_quantity": qty,
            "stock_status":   "onbackorder",
            "backorders":     "notify",
            "meta_data": [
                {
                    "key":   "_wclt_variation_lead_time",
                    "value": backorder_msg,
                },
                {
                    "key":   "_wclt_lead_time_backorder",
                    "value": backorder_msg,
                },
            ],
        }
    
    # ── Out of Stock: SKU starts with 49 → Backorder ──
    if str(raw_sku).strip().startswith("49"):
        backorder_msg = "Innerhalb von 3-4 Tagen lieferbar"

        return {
            "regular_price":  price_str,
            "price":          price_str,
            "manage_stock":   True,
            "stock_quantity": qty,
            "stock_status":   "onbackorder",
            "backorders":     "notify",
            "meta_data": [
                {
                    "key":   "_wclt_variation_lead_time",
                    "value": backorder_msg,
                },
                {
                    "key":   "_wclt_lead_time_backorder",
                    "value": backorder_msg,
                },
            ],
        }

    # ── Out of Stock: all other SKUs ──────────────
    outofstock_msg = (
        "Dieser Artikel ist heute noch nicht lagerhaltig. "
        "Viele Artikel können wir dennoch innert Kürze ausliefern. "
        "Bitte kontaktieren sie uns."
    )
    return {
        "regular_price":  price_str,
        "price":          price_str,
        "manage_stock":   True,
        "stock_quantity": qty,
        "stock_status":   "outofstock",
        "backorders":     "no",
        "meta_data": [
            {
                "key":   "_wclt_variation_lead_time",
                "value": outofstock_msg,
            },
            {
                "key":   "_wclt_lead_time_outofstock",
                "value": outofstock_msg,
            },
        ],
    }


# ─────────────────────────────────────────────────
# 7. UPDATE ONE WC PRODUCT/VARIATION
# ─────────────────────────────────────────────────
def update_wc_item(match: dict, payload: dict) -> dict:
    """
    Sends a PUT request to update price/stock for one matched item.
    Handles both simple products and variations.
    Returns a result dict describing success/failure.
    """
    product_id   = match["product_id"]
    variation_id = match.get("variation_id")

    if variation_id:
        url = f"{WC_BASE_URL}/wp-json/wc/v3/products/{product_id}/variations/{variation_id}"
    else:
        url = f"{WC_BASE_URL}/wp-json/wc/v3/products/{product_id}"

    try:
        resp = wc_put_with_retry(url, payload)
        logger.info(
            "    ✅ Updated product=%s variation=%s → status=%s",
            product_id, variation_id, resp.status_code,
        )
        return {"success": True, "status_code": resp.status_code, "error": None}
    except Exception as exc:
        logger.error(
            "    ❌ Failed product=%s variation=%s → %s",
            product_id, variation_id, exc,
        )
        return {"success": False, "status_code": None, "error": str(exc)}


# ─────────────────────────────────────────────────
# 8. MAIN PIPELINE — returns a summary dict
# ─────────────────────────────────────────────────
def run_sync() -> dict:
    """
    Full ERP → WooCommerce sync pipeline.
    Returns a summary dict with counts and any per-item errors.
    Does NOT write any file to disk.
    """
    summary = {
        "erp_total":       0,
        "wc_matched":      0,
        "wc_updated_ok":   0,
        "wc_update_failed": 0,
        "errors":          [],   # list of {"sku": ..., "error": ...}
    }

    # ── Step 1: ERP ──────────────────────────────
    erp_data = fetch_erp_data()
    if not erp_data:
        raise RuntimeError("Could not fetch ERP data — aborting sync.")

    erp_lookup = build_erp_lookup(erp_data)
    summary["erp_total"] = len(erp_lookup)

    # ── Step 2: WooCommerce ──────────────────────
    all_products = fetch_all_wc_products()
    sku_map      = build_sku_map(all_products)

    # ── Step 3: Match + Update ───────────────────
    logger.info("[SYNC] Matching and updating %d ERP articles…", len(erp_lookup))
    total = len(erp_lookup)

    for idx, (norm_key, erp_item) in enumerate(erp_lookup.items(), start=1):
        raw_sku   = str(erp_item.get("ArticleNumber", "")).strip()
        clean_sku = clean(raw_sku)

        matches = (
            sku_map.get(raw_sku)
            or sku_map.get(clean_sku)
            or sku_map.get(norm_key)
        )

        if not matches:
            # ── No match → skip entirely (no JSON entry, no update)
            logger.debug("  [%d/%d] '%s' → ❌ No match — skipping", idx, total, raw_sku)
            continue

        erp_price       = erp_item.get("SalesPriceNet")
        erp_quantity    = erp_item.get("Quantity")
        erp_description = erp_item.get("DescriptionUnit")
        payload         = build_update_payload(erp_price, erp_quantity, erp_description, raw_sku)

        for match in matches:
            summary["wc_matched"] += 1
            logger.info(
                "  [%d/%d] '%s' → ✅ Product=%s | Variation=%s | Qty=%s | Price=%s",
                idx, total, raw_sku,
                match["product_id"], match["variation_id"],
                erp_quantity, erp_price,
            )
            result = update_wc_item(match, payload)
            if result["success"]:
                summary["wc_updated_ok"] += 1
            else:
                summary["wc_update_failed"] += 1
                summary["errors"].append({
                    "sku":          raw_sku,
                    "product_id":   match["product_id"],
                    "variation_id": match["variation_id"],
                    "error":        result["error"],
                })

    logger.info(
        "[SYNC] Done — ERP: %d | Matched: %d | Updated OK: %d | Failed: %d",
        summary["erp_total"],
        summary["wc_matched"],
        summary["wc_updated_ok"],
        summary["wc_update_failed"],
    )
    return summary


# ─────────────────────────────────────────────────
# Run standalone (for testing without FastAPI)
# ─────────────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    result = run_sync()
    print("\n" + "=" * 55)
    print("✅  Sync complete")
    print(f"   ERP articles     : {result['erp_total']}")
    print(f"   WC matched       : {result['wc_matched']}")
    print(f"   Updated OK       : {result['wc_updated_ok']}")
    print(f"   Update failures  : {result['wc_update_failed']}")
    if result["errors"]:
        print(f"\n   ⚠️  Errors ({len(result['errors'])}):")
        for e in result["errors"]:
            print(f"      SKU={e['sku']} P={e['product_id']} V={e['variation_id']}: {e['error']}")
    print("=" * 55)