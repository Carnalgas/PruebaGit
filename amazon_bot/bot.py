#!/usr/bin/env python3
"""Bot de compra automática en Amazon (Playwright).

Compra los productos de products.json cuando están disponibles y su precio
es <= max_price. Por defecto NO compra (modo simulación); hay que pasar --live.
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import Page, TimeoutError as PWTimeout, sync_playwright

HERE = Path(__file__).parent
PROFILE_DIR = HERE / ".browser_profile"   # sesión de Amazon persistente (no se commitea)
STATE_FILE = HERE / ".purchased.json"     # evita comprar dos veces lo mismo


def parse_price(text: str):
    """'1.234,56 €' / '$1,234.56' -> 1234.56"""
    m = re.search(r"\d[\d.,\s]*", text or "")
    if not m:
        return None
    s = m.group(0).strip().replace(" ", "")
    if "," in s and "." in s:
        dec = "," if s.rfind(",") > s.rfind(".") else "."
    elif "," in s:
        dec = "," if len(s.split(",")[-1]) <= 2 else ""
    elif "." in s:
        dec = "." if len(s.split(".")[-1]) <= 2 else ""
    else:
        dec = ""
    if dec:
        ent, frac = s.rsplit(dec, 1)
        ent = re.sub(r"[.,]", "", ent)
        return float(f"{ent}.{frac}")
    return float(re.sub(r"[.,]", "", s))


def load_state():
    return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def product_url(p, domain):
    return p.get("url") or f"https://{domain}/dp/{p['asin']}"


def product_key(p):
    return p.get("asin") or p["url"]


def first_text(page: Page, selectors):
    for sel in selectors:
        loc = page.locator(sel).first
        if loc.count():
            t = loc.inner_text().strip() if "offscreen" not in sel else loc.text_content().strip()
            if t:
                return t
    return None


def current_price(page: Page):
    t = first_text(page, [
        "#corePrice_feature_div .a-price .a-offscreen",
        "#corePriceDisplay_desktop_feature_div .a-price .a-offscreen",
        "#price_inside_buybox",
        "#priceblock_ourprice",
        "#priceblock_dealprice",
        ".a-price .a-offscreen",
    ])
    return parse_price(t) if t else None


def is_logged_in(page: Page) -> bool:
    return page.locator("#nav-link-accountList-nav-line-1, #nav-link-accountList").count() > 0 and \
        "Sign in" not in (page.locator("#nav-link-accountList-nav-line-1").first.text_content() or "") and \
        "identifícate" not in (page.locator("#nav-link-accountList-nav-line-1").first.text_content() or "").lower()


def ensure_login(page: Page, domain: str):
    page.goto(f"https://{domain}/", wait_until="domcontentloaded")
    if is_logged_in(page):
        return
    print("\n>>> Inicia sesión en Amazon en la ventana del navegador (incluye 2FA/captcha).")
    print(">>> Cuando termines, pulsa ENTER aquí.")
    page.goto(f"https://{domain}/gp/sign-in.html", wait_until="domcontentloaded")
    input()
    page.goto(f"https://{domain}/", wait_until="domcontentloaded")
    if not is_logged_in(page):
        sys.exit("No se detecta sesión iniciada. Abortando.")


def try_buy(page: Page, p: dict, domain: str, live: bool) -> str:
    """Devuelve: 'bought' | 'dry-run' | 'skip:<motivo>'."""
    name = p.get("name", product_key(p))
    page.goto(product_url(p, domain), wait_until="domcontentloaded")
    if page.locator("form[action*='validateCaptcha']").count():
        return "skip:captcha (resuélvelo manualmente)"

    price = current_price(page)
    if price is None:
        return "skip:sin precio / no disponible"
    if price > p["max_price"]:
        return f"skip:precio {price:.2f} > máximo {p['max_price']:.2f}"

    qty = int(p.get("quantity", 1))
    if qty > 1 and page.locator("#quantity").count():
        page.select_option("#quantity", str(qty))

    buy_now = page.locator("#buy-now-button")
    add_cart = page.locator("#add-to-cart-button")
    if buy_now.count():
        buy_now.first.click()
    elif add_cart.count():
        add_cart.first.click()
        page.goto(f"https://{domain}/gp/cart/view.html", wait_until="domcontentloaded")
        page.locator("input[name='proceedToRetailCheckout']").first.click()
    else:
        return "skip:sin stock (no hay botón de compra)"

    # Pantalla de revisión del pedido
    try:
        page.wait_for_selector("input[name='placeYourOrder1'], #placeOrder, #submitOrderButtonId",
                               timeout=20000)
    except PWTimeout:
        return "skip:no se llegó a la pantalla final (¿dirección/pago pendiente o verificación?)"

    total_txt = first_text(page, [".grand-total-price", "#subtotals-marketplace-table .grand-total-price",
                                  "#bottomSubmitOrderButtonId-announce"]) 
    total = parse_price(total_txt) if total_txt else None
    limit = p["max_price"] * qty
    if total is not None and total > limit * 1.0001 + p.get("max_extra", 0):
        return f"skip:total pedido {total:.2f} > límite {limit:.2f}"

    if not live:
        return f"dry-run:{name} listo para comprar (precio {price:.2f}, total {total})"

    page.locator("input[name='placeYourOrder1'], #placeOrder, #submitOrderButtonId").first.click()
    try:
        page.wait_for_url(re.compile(r"(thankyou|order-confirmation|/buy/thankyou)"), timeout=30000)
    except PWTimeout:
        return "skip:no se pudo confirmar el pedido; revisa tus pedidos en Amazon"
    return "bought"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=str(HERE / "products.json"))
    ap.add_argument("--live", action="store_true", help="COMPRAR de verdad (si no, simulación)")
    ap.add_argument("--watch", type=int, metavar="SEG", help="repetir cada SEG segundos hasta comprar todo")
    ap.add_argument("--headless", action="store_true", help="sin ventana (requiere sesión ya iniciada)")
    args = ap.parse_args()

    cfg = json.loads(Path(args.config).read_text())
    domain = cfg.get("domain", "www.amazon.es")
    products = cfg["products"]
    for p in products:
        if "max_price" not in p or not (p.get("url") or p.get("asin")):
            sys.exit(f"Producto inválido (necesita url/asin y max_price): {p}")

    state = load_state()
    if args.live:
        print("MODO REAL: se realizarán compras.")
    else:
        print("Modo simulación: no se comprará nada (usa --live).")

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            str(PROFILE_DIR), headless=args.headless, locale="es-ES",
            viewport={"width": 1280, "height": 900})
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        ensure_login(page, domain)

        while True:
            pending = [p for p in products if product_key(p) not in state]
            if not pending:
                print("Todo comprado.")
                break
            for p in pending:
                res = try_buy(page, p, domain, args.live)
                print(f"[{time.strftime('%H:%M:%S')}] {p.get('name', product_key(p))}: {res}")
                if res == "bought":
                    state[product_key(p)] = time.strftime("%Y-%m-%d %H:%M:%S")
                    save_state(state)
            if not args.watch:
                break
            time.sleep(args.watch)
        ctx.close()


if __name__ == "__main__":
    main()
