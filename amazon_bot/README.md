# Bot de compras automáticas en Amazon

Compra los productos que indiques cuando estén en stock y por debajo del precio máximo.

## Instalación
```bash
cd amazon_bot
pip install -r requirements.txt
playwright install chromium
cp products.example.json products.json   # edítalo con tus productos
```

## Uso
```bash
python bot.py                     # simulación (NO compra): llega hasta "Realizar pedido"
python bot.py --live              # compra de verdad
python bot.py --live --watch 300  # vigila cada 5 min y compra al cumplirse las condiciones
```
La primera vez se abre un navegador: inicia sesión tú mismo (2FA incluido). La sesión
se guarda en `.browser_profile/` (ignorado por git); el bot **nunca ve ni guarda tu contraseña**.

## products.json
| campo | descripción |
|---|---|
| `domain` | tienda (`www.amazon.es`, `www.amazon.com`, ...) |
| `url` o `asin` | producto |
| `max_price` | precio máximo por unidad; también se verifica el total del pedido |
| `quantity` | unidades (por defecto 1) |
| `allow_used` | `true` para aceptar ofertas usadas/reacondicionadas en "Ver todas las opciones de compra" (por defecto `false`) |
| `seller` | texto que debe contener el vendedor, p. ej. `"Amazon"` (opcional) |
| `max_extra` | margen extra permitido para envío en el total (por defecto 0) |

Si el producto no tiene botón de compra, o el precio principal supera `max_price`, el bot abre
"Ver todas las opciones de compra" y elige la oferta más barata (precio + envío) que cumpla las
condiciones. Por esa vía solo se admite `quantity` 1.

## Seguridad
- Por defecto es simulación; `--live` es explícito.
- Cada producto se compra una sola vez (`.purchased.json`).
- Usa tu dirección y método de pago predeterminados de Amazon: revísalos antes.
- Prueba primero sin `--live`. Los selectores HTML de Amazon cambian; si algo falla, el bot
  omite la compra en lugar de arriesgarse.
- Aviso: las condiciones de uso de Amazon restringen la automatización; úsalo bajo tu
  responsabilidad y con frecuencias razonables. Amazon puede mostrar captchas.
