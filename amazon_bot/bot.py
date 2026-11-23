#!/usr/bin/env python3
"""Bot de compra automática en Amazon (Playwright).

Compra los productos de products.json cuando están disponibles y su precio
es <= max_price. Por defecto NO compra (modo simulación); hay que pasar --live.
"""
import argparse
import json
import random
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import Error as PWError, Page, TimeoutError as PWTimeout, sync_playwright

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


def safe_goto(page: Page, url: str, tries: int = 3):
    """page.goto tolerante a las redirecciones de Amazon (navegación interrumpida / ERR_ABORTED)."""
    for _ in range(tries):
        try:
            page.goto(url, wait_until="domcontentloaded")
            return
        except PWError as e:
            msg = str(e)
            if "interrupted by another navigation" not in msg and "ERR_ABORTED" not in msg:
                raise
            try:
                page.wait_for_load_state("domcontentloaded", timeout=15000)
            except (PWError, PWTimeout):
                pass
            time.sleep(2)
    page.goto(url, wait_until="domcontentloaded")


def logged_in_now(page: Page) -> bool:
    try:
        url = page.url
        return "/ap/" not in url and "/ax/" not in url and is_logged_in(page)
    except PWError:
        return False  # la página está a mitad de una navegación


def ensure_login(page: Page, domain: str, wait_seconds: int = 300):
    safe_goto(page, f"https://{domain}/")
    if logged_in_now(page):
        return
    print("\n>>> Inicia sesión en Amazon en la ventana del navegador (incluye 2FA/captcha).")
    print(f">>> No toques la consola: el bot sigue solo al detectar la sesión (espera hasta {wait_seconds // 60} min).")
    try:
        page.locator("#nav-link-accountList").first.click(timeout=10000)
    except (PWError, PWTimeout):
        pass  # pulsa tú "Hola, identifícate" en la ventana
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        time.sleep(2)
        if logged_in_now(page):
            print("Sesión detectada.")
            return
    sys.exit("No se detectó sesión iniciada a tiempo. Vuelve a ejecutar el bot.")


def product_asin(p: dict):
    if p.get("asin"):
        return p["asin"]
    m = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})", p.get("url", ""))
    return m.group(1) if m else None


def open_all_offers(page: Page, p: dict, domain: str) -> bool:
    """Abre el panel 'Ver todas las opciones de compra'. True si se ve al menos una oferta."""
    offers_sel = "#aod-pinned-offer, #aod-offer-list #aod-offer"
    link = page.locator("#buybox-see-all-buying-choices a, a#aod-ingress-link, "
                        "#aod-ingress-link a, a[href*='aod=1']")
    try:
        if link.count():
            link.first.click(timeout=5000)
        else:
            asin = product_asin(p)
            if not asin:
                return False
            safe_goto(page, f"https://{domain}/dp/{asin}?aod=1")
        page.wait_for_selector(offers_sel, timeout=10000)
        page.wait_for_timeout(1500)  # las ofertas terminan de cargar por partes
        return True
    except (PWError, PWTimeout):
        return False


ADD_TO_CART_SELECTORS = (
    "input[name='submit.addToCart']",
    "input[name*='addToCart']",
    "button[name*='addToCart']",
    "[id*='aod-atc'] input, [id*='aod-atc'] button",
    "[data-action*='atc'] input, [data-action*='atc'] button",
)
ADD_TO_CART_TEXT = re.compile(r"agregar al carrito|añadir al carrito|add to cart", re.I)


def offer_add_button(offer):
    """Botón 'Agregar al carrito' de una oferta del panel (Amazon cambia sus nombres)."""
    for sel in ADD_TO_CART_SELECTORS:
        loc = offer.locator(sel)
        if loc.count():
            return loc.first
    loc = offer.get_by_role("button", name=ADD_TO_CART_TEXT)
    if loc.count():
        return loc.first
    loc = offer.locator("input[type='submit'][aria-labelledby]")
    for i in range(loc.count()):
        lab_id = loc.nth(i).get_attribute("aria-labelledby") or ""
        lab = offer.locator(f"[id='{lab_id}']")
        if lab.count() and ADD_TO_CART_TEXT.search(lab.first.text_content() or ""):
            return loc.nth(i)
    return None


DEBUG_DIR = HERE / "debug"


def save_debug(page: Page, tag: str):
    """Guarda captura y HTML para poder ajustar los selectores."""
    try:
        DEBUG_DIR.mkdir(exist_ok=True)
        base = DEBUG_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}_{tag}"
        page.screenshot(path=f"{base}.png", full_page=True)
        Path(f"{base}.html").write_text(page.content(), encoding="utf-8")
        print(f"  (diagnóstico guardado en {base}.png / .html)")
    except PWError:
        pass


def cart_count(page: Page):
    try:
        t = page.locator("#nav-cart-count").first.text_content(timeout=1000)
        return int(re.sub(r"\D", "", t or "") or 0)
    except (PWError, PWTimeout, ValueError):
        return None


def go_to_checkout(page: Page, domain: str) -> bool:
    safe_goto(page, f"https://{domain}/gp/cart/view.html")
    btn = page.locator("input[name='proceedToRetailCheckout'], #sc-buy-box-ptc-button input, "
                       "#sc-buy-box-ptc-button button")
    try:
        btn.first.click(timeout=10000)
        return True
    except (PWError, PWTimeout):
        save_debug(page, "carrito_sin_boton_pago")
        return False


def best_offer(page: Page, p: dict):
    """Busca en el panel de ofertas la más barata que cumpla las condiciones.
    Devuelve (oferta, precio, envío, vendedor) o None."""
    allow_used = p.get("allow_used", False)
    seller_filter = (p.get("seller") or "").lower()
    limit = p["max_price"] + p.get("max_extra", 0)
    best = None
    no_button = 0
    offers = page.locator("#aod-pinned-offer, #aod-offer-list #aod-offer")
    total_offers = offers.count()
    for i in range(total_offers):
        o = offers.nth(i)
        try:
            add = offer_add_button(o)
            if add is None:
                no_button += 1
                continue
            price_el = o.locator(".a-price .a-offscreen").first
            price = parse_price(price_el.text_content() if price_el.count() else "")
            if price is None:
                continue
            heading = (o.locator("#aod-offer-heading").first.text_content() or "") \
                if o.locator("#aod-offer-heading").count() else ""
            if not allow_used and re.search(r"usado|used|reacondicionado|renewed", heading, re.I):
                continue
            seller_el = o.locator("#aod-offer-soldBy a, #aod-offer-soldBy .a-col-right").first
            seller = (seller_el.text_content() or "").strip() if seller_el.count() else ""
            if seller_filter and seller_filter not in seller.lower():
                continue
            ship = 0.0
            ship_el = o.locator("[data-csa-c-delivery-price]").first
            if ship_el.count():
                ship_txt = ship_el.get_attribute("data-csa-c-delivery-price") or ""
                if not re.search(r"gratis|free", ship_txt, re.I):
                    ship = parse_price(ship_txt) or 0.0
            if price > p["max_price"] or price + ship > limit:
                continue
            if best is None or price + ship < best[1] + best[2]:
                best = (add, price, ship, seller or "?")
        except PWError:
            continue
    if best is None and total_offers and no_button == total_offers:
        save_debug(page, "aod_sin_boton")
    return best


# --- Carga ligera: no descargar lo que no hace falta para ver precio y stock ---
BLOCK_TYPES = {"image", "media", "font"}
BLOCK_HOSTS = ("amazon-adsystem.com", "doubleclick.net", "googlesyndication.com",
               "googletagservices.com", "adnxs.com")


class Blocker:
    """Bloquea imágenes, fuentes, videos y anuncios. Se apaga para iniciar sesión o
    resolver un captcha, porque ahí sí necesitas ver las imágenes."""
    def __init__(self, enabled: bool):
        self.enabled = enabled

    def install(self, ctx):
        def handler(route):
            req = route.request
            if self.enabled and (req.resource_type in BLOCK_TYPES
                                 or any(h in req.url for h in BLOCK_HOSTS)):
                return route.abort()
            return route.continue_()
        ctx.route("**/*", handler)


def has_captcha(page: Page) -> bool:
    try:
        return page.locator("form[action*='validateCaptcha']").count() > 0
    except PWError:
        return False


def wait_captcha_solved(page: Page, blocker: Blocker, headless: bool, wait_seconds: int = 300) -> bool:
    """Recarga con imágenes para que puedas resolver el captcha en la ventana."""
    if headless:
        return False
    prev = blocker.enabled
    blocker.enabled = False
    try:
        page.reload(wait_until="domcontentloaded")
    except PWError:
        pass
    print("\a>>> Amazon pidió un captcha: resuélvelo en la ventana del navegador.")
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        time.sleep(2)
        if not has_captcha(page):
            blocker.enabled = prev
            print("Captcha resuelto, continúo.")
            return True
    blocker.enabled = prev
    return False


def try_buy(page: Page, p: dict, domain: str, live: bool) -> str:
    """Devuelve: 'bought' | 'dry-run' | 'skip:<motivo>'."""
    name = p.get("name", product_key(p))
    safe_goto(page, product_url(p, domain))
    if has_captcha(page):
        return "captcha"

    qty = int(p.get("quantity", 1))
    price = current_price(page)
    buy_now = page.locator("#buy-now-button")
    add_cart = page.locator("#add-to-cart-button")
    has_buybox = bool(buy_now.count() or add_cart.count())
    source = "botón principal"

    if has_buybox and price is not None and price <= p["max_price"]:
        if qty > 1 and page.locator("#quantity").count():
            page.select_option("#quantity", str(qty))
        if buy_now.count():
            buy_now.first.click()
        else:
            add_cart.first.click()
            if not go_to_checkout(page, domain):
                return "skip:no encontré 'Proceder al pago' en el carrito"
    else:
        # Sin botón de compra, o el precio principal es caro: revisar "Ver todas las opciones de compra".
        why = "sin botón de compra" if not has_buybox else \
            ("sin precio" if price is None else f"precio principal {price:.2f} > máximo {p['max_price']:.2f}")
        if not open_all_offers(page, p, domain):
            return f"skip:{why} y no hay otras ofertas"
        offer = best_offer(page, p)
        if offer is None:
            return (f"skip:{why} y ninguna oferta en 'opciones de compra' cumple las condiciones"
                    " (si dice 'diagnóstico guardado', mándame esos archivos)")
        if qty > 1:
            return "skip:hay oferta en 'opciones de compra' pero solo se admite quantity 1 por esa vía"
        button, price, ship, seller = offer
        source = f"opciones de compra (vendedor: {seller}, envío {ship:.2f})"
        before = cart_count(page)
        button.scroll_into_view_if_needed()
        button.click()
        # Esperar a que Amazon confirme que se agregó (sube el contador del carrito).
        for _ in range(16):
            page.wait_for_timeout(500)
            now = cart_count(page)
            if before is not None and now is not None and now > before:
                break
        if not go_to_checkout(page, domain):
            return "skip:no encontré 'Proceder al pago' en el carrito"

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
        return f"dry-run:{name} listo para comprar vía {source} (precio {price:.2f}, total {total})"

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
    ap.add_argument("--no-block", action="store_true",
                    help="cargar imágenes/fuentes/anuncios (más lento; úsalo si algo no se detecta bien)")
    args = ap.parse_args()

    cfg = json.loads(Path(args.config).read_text())
    domain = cfg.get("domain", "www.amazon.es")
    locale = "es-MX" if domain.endswith(".mx") else ("en-US" if domain.endswith(".com") else "es-ES")
    products = cfg["products"]
    for p in products:
        if "max_price" not in p or not (p.get("url") or p.get("asin")):
            sys.exit(f"Producto inválido (necesita url/asin y max_price): {p}")

    MIN_WATCH = 20
    if args.watch and args.watch < MIN_WATCH:
        print(f"--watch {args.watch} es muy agresivo y provoca captchas; uso {MIN_WATCH} s.")
        args.watch = MIN_WATCH

    state = load_state()
    if args.live:
        print("MODO REAL: se realizarán compras.")
    else:
        print("Modo simulación: no se comprará nada (usa --live).")

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            str(PROFILE_DIR), headless=args.headless, locale=locale,
            viewport={"width": 1280, "height": 900})
        blocker = Blocker(enabled=False)       # apagado para iniciar sesión
        blocker.install(ctx)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        ensure_login(page, domain)
        blocker.enabled = not args.no_block

        backoff = 1                            # se multiplica tras cada captcha
        while True:
            pending = [p for p in products if product_key(p) not in state]
            if not pending:
                print("Todo comprado.")
                break
            captcha_this_round = False
            for i, p in enumerate(pending):
                if i:
                    time.sleep(random.uniform(1.5, 4))   # pausa breve entre productos
                t0 = time.time()
                res = try_buy(page, p, domain, args.live)
                if res == "captcha":
                    captcha_this_round = True
                    if wait_captcha_solved(page, blocker, args.headless):
                        res = try_buy(page, p, domain, args.live)
                    else:
                        res = "skip:captcha sin resolver"
                print(f"[{time.strftime('%H:%M:%S')}] {p.get('name', product_key(p))}: {res} "
                      f"({time.time() - t0:.1f} s)")
                if res == "bought":
                    state[product_key(p)] = time.strftime("%Y-%m-%d %H:%M:%S")
                    save_state(state)
            if not args.watch:
                break
            backoff = min(backoff * 2, 8) if captcha_this_round else max(1, backoff // 2)
            wait = args.watch * backoff * random.uniform(0.8, 1.25)
            if backoff > 1:
                print(f"Hubo captcha: bajo el ritmo, próxima revisión en {wait:.0f} s.")
            time.sleep(wait)
        ctx.close()


if __name__ == "__main__":
    main()
