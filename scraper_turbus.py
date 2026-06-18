#!/usr/bin/env python3
"""
Scraper para obtener horarios de buses en Turbus.cl

Funcionamiento general:
  1. Abre un navegador Chrome controlado por Selenium (puede ser local o remoto via Docker).
  2. Carga la URL de búsqueda de Turbus y espera a que el JavaScript de la página
     termine de renderizar los resultados dinámicos.
  3. Extrae el HTML ya renderizado y lo analiza con BeautifulSoup.
  4. Busca los bloques de cada servicio de bus y extrae la hora de salida.
  5. Normaliza las horas al formato 24h, elimina duplicados, ordena y muestra en consola.

Uso:
  python scraper_turbus.py [--url URL] [--wait SEGUNDOS] [--no-headless] [--selenium-url URL]

Dependencias:
  pip install selenium beautifulsoup4 chromedriver_autoinstaller

Nota sobre chromedriver:
  Si ejecutas localmente, chromedriver_autoinstaller descargará el driver compatible
  con tu versión de Chrome automáticamente. Si usas Docker, pasa la URL del servidor
  Selenium con --selenium-url (ej. http://localhost:4444/wd/hub).
"""

import argparse
import os
import re
import sys
import time
from datetime import datetime, date

from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.desired_capabilities import DesiredCapabilities
from selenium.webdriver.common.by import By


# ---------------------------------------------------------------------------
# Utilidades para procesar precios
# ---------------------------------------------------------------------------

def parse_price(price_str: str) -> int:
    """
    Convierte un string de precio (ej. "$4.500") a un número entero para comparación.
    Retorna el valor numérico sin símbolos ni puntos.
    Si no puede parsear, retorna 0.
    """
    try:
        # Elimina símbolos de moneda, espacios y puntos separadores de miles
        clean = price_str.replace("$", "").replace(" ", "").replace(".", "")
        return int(clean) if clean else 0
    except (ValueError, AttributeError):
        return 0


# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------

def log(level: str, msg: str) -> None:
    """
    Imprime un mensaje con timestamp y etiqueta de nivel.

    Niveles usados:
      STEP  → paso principal del flujo (qué estamos haciendo ahora)
      INFO  → información secundaria o detalles de un item
      WARN  → algo inesperado pero recuperable
      ERROR → fallo crítico
      OK    → operación exitosa
    """
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [{level:5s}] {msg}")


def abort(msg: str, driver=None, exit_code: int = 1, cause: str | Exception | None = None) -> None:
    """
    Detiene el proceso ante un fallo lógico (sin excepción).
    Cierra el navegador si está abierto y sale con código distinto de cero.
    """
    log("ERROR", msg)
    if cause is not None:
        if isinstance(cause, Exception):
            log("ERROR", f"Detalle: {type(cause).__name__}: {cause}")
        else:
            log("ERROR", f"Detalle: {cause}")
    if driver:
        try:
            driver.quit()
        except Exception:
            pass
    sys.exit(exit_code)


# ---------------------------------------------------------------------------
# Expresión regular para detectar horas en formato HH:MM (con AM/PM opcional)
# ---------------------------------------------------------------------------

# Captura grupos:
#   grupo 1 → horas   (1 o 2 dígitos)
#   grupo 2 → minutos (exactamente 2 dígitos, 00-59)
#   grupo 3 → sufijo AM/PM opcional (con o sin espacio antes)
TIME_RE = re.compile(r"\b([0-2]?\d):([0-5]\d)\s*([AaPp][Mm])?\b")


# ---------------------------------------------------------------------------
# Instalación del chromedriver local
# ---------------------------------------------------------------------------

def install_driver() -> str:
    """
    Usa chromedriver_autoinstaller para descargar (si es necesario) el chromedriver
    cuya versión coincide exactamente con el Chrome instalado en la máquina.

    Retorna la ruta al ejecutable de chromedriver.
    Lanza RuntimeError si chromedriver_autoinstaller no está disponible.
    """
    try:
        import chromedriver_autoinstaller
    except ImportError:
        raise RuntimeError(
            "Falta la dependencia 'chromedriver_autoinstaller'. "
            "Instala con: pip install chromedriver_autoinstaller"
        )

    log("STEP", "Verificando o instalando chromedriver local...")
    path = chromedriver_autoinstaller.install()
    log("OK", f"chromedriver listo en: {path}")
    return path


# ---------------------------------------------------------------------------
# Renderizado de la página con Selenium
# ---------------------------------------------------------------------------

def render_page(url: str, wait_seconds: int = 8, headless: bool = True, selenium_url: str | None = None) -> str:
    """
    Abre la URL en Chrome (local o remoto) y devuelve el HTML completamente renderizado.

    Parámetros:
      url           → dirección web a cargar
      wait_seconds  → segundos adicionales para que React/Vue/etc. termine de dibujar los resultados
      headless      → True = Chrome sin ventana (ideal para servidores); False = con UI (para depurar)
      selenium_url  → si se indica, usa un servidor Selenium remoto en lugar de Chrome local
                      (ej. "http://localhost:4444/wd/hub" cuando usas Docker)
    """

    # --- Opciones de Chrome comunes para correr en entornos sin pantalla -------
    chrome_options = Options()

    if headless:
        # '--headless=new' es la bandera moderna; la antigua '--headless' tiene bugs con algunos sitios
        chrome_options.add_argument("--headless=new")
        log("INFO", "Modo headless activado (Chrome sin ventana)")
    else:
        log("INFO", "Modo con ventana activado (útil para depuración manual)")

    # Necesario para correr Chrome como root o en contenedores Docker
    chrome_options.add_argument("--no-sandbox")

    # Evita que Chrome crashee en contenedores con /dev/shm pequeño
    chrome_options.add_argument("--disable-dev-shm-usage")

    # Deshabilita aceleración GPU (no disponible en servidores)
    chrome_options.add_argument("--disable-gpu")

    # User-agent que imita un navegador real para evitar bloqueos anti-bot
    chrome_options.add_argument(
        "--user-agent=Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0 Safari/537.36"
    )

    # --- Inicialización del driver (remoto o local) ----------------------------
    driver = None
    try:
        if selenium_url:
            # Modo remoto: conecta a un servidor Selenium ya corriendo
            # Útil con: docker run -d -p 4444:4444 selenium/standalone-chrome
            log("STEP", f"Conectando a servidor Selenium remoto: {selenium_url}")
            try:
                driver = webdriver.Remote(command_executor=selenium_url, options=chrome_options)
            except TypeError:
                # Versiones antiguas del servidor Selenium no aceptan 'options';
                # fallback a 'desired_capabilities' (API deprecada pero compatible)
                log("WARN", "El servidor remoto no aceptó 'options'; reintentando con desired_capabilities...")
                caps = DesiredCapabilities.CHROME.copy()
                driver = webdriver.Remote(command_executor=selenium_url, desired_capabilities=caps)
        else:
            # Modo local: instala chromedriver si es necesario y lo usa
            log("STEP", "Conectando a chromedriver local...")
            driver_path = install_driver()
            service = Service(driver_path)
            driver = webdriver.Chrome(service=service, options=chrome_options)

        log("STEP", f"Cargando URL: {url}")
        driver.set_page_load_timeout(30)
        driver.get(url)

        # Esperar a que document.readyState sea 'complete'
        log("INFO", "Esperando a que la página se cargue completamente...")
        for attempt in range(10):
            ready = driver.execute_script("return document.readyState")
            log("INFO", f"  Intento {attempt + 1}/10: readyState = {ready}")
            if ready == "complete":
                break
            time.sleep(0.5)

        # Espera adicional para JS/React
        log("INFO", f"Esperando {wait_seconds}s para renderizado dinámico...")
        time.sleep(wait_seconds)

        return driver.page_source
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


def open_browser(url: str, wait_seconds: int = 8, headless: bool = True, selenium_url: str | None = None):
    """
    Abre un navegador y carga la URL, pero retorna el driver en lugar de cerrarlo.
    Esto permite interactuar con la página de forma dinámica (clicks, etc.).
    
    IMPORTANTE: El llamador es responsable de cerrar el driver cuando termine.
    
    Retorna el objeto WebDriver.
    """
    chrome_options = Options()

    if headless:
        chrome_options.add_argument("--headless=new")
        log("INFO", "Modo headless activado (Chrome sin ventana)")
    else:
        log("INFO", "Modo con ventana activado (útil para depuración manual)")

    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument(
        "--user-agent=Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0 Safari/537.36"
    )

    if selenium_url:
        log("STEP", f"Conectando a servidor Selenium remoto: {selenium_url}")
        try:
            driver = webdriver.Remote(command_executor=selenium_url, options=chrome_options)
        except TypeError:
            log("WARN", "Usando desired_capabilities...")
            caps = DesiredCapabilities.CHROME.copy()
            driver = webdriver.Remote(command_executor=selenium_url, desired_capabilities=caps)
    else:
        log("STEP", "Conectando a chromedriver local...")
        driver_path = install_driver()
        service = Service(driver_path)
        driver = webdriver.Chrome(service=service, options=chrome_options)

    log("STEP", f"Cargando URL: {url}")
    driver.set_page_load_timeout(30)
    driver.get(url)

    log("INFO", "Esperando a que la página se cargue completamente...")
    for attempt in range(10):
        ready = driver.execute_script("return document.readyState")
        if ready == "complete":
            break
        time.sleep(0.5)

    log("INFO", f"Esperando {wait_seconds}s para renderizado dinámico...")
    time.sleep(wait_seconds)

    return driver


# ---------------------------------------------------------------------------
# Interacción con asientos del bus
# ---------------------------------------------------------------------------

def extract_available_seats(driver) -> tuple[list[str], bool, str | None]:
    """
    Extrae los números de asientos disponibles del layout actual del bus.
    
    Busca elementos con clase 'icon-semi-bed-seat_available' y extrae el número
    del asiento desde el elemento <span> con clase 'seat_number__EfiN0'.
    
    Retorna (lista de asientos, layout_encontrado, detalle_error).
    """
    try:
        soup = BeautifulSoup(driver.page_source, "html.parser")
        available_seats = []
        
        available_divs = soup.find_all(class_="icon-semi-bed-seat_available")
        seat_numbers = soup.find_all(class_="seat_number__EfiN0")
        layout_found = bool(seat_numbers or available_divs)
        
        for seat_div in available_divs:
            li_parent = seat_div.find_parent('li')
            if li_parent:
                seat_num_span = li_parent.find(class_="seat_number__EfiN0")
                if seat_num_span:
                    seat_num = seat_num_span.get_text(strip=True)
                    available_seats.append(seat_num)
        
        if not layout_found:
            detail = (
                f"No se encontraron elementos del mapa de asientos "
                f"(seat_number={len(seat_numbers)}, disponibles={len(available_divs)}, "
                f"URL={driver.current_url})"
            )
            return available_seats, False, detail

        return available_seats, True, None
    except Exception as e:
        return [], False, e


def click_purchase_button(driver) -> tuple[bool, str | Exception | None]:
    """
    Hace click en el botón "Comprar" para ver los asientos disponibles.
    
    Retorna (éxito, detalle_error).
    """
    try:
        button = driver.find_element(By.XPATH, "//button[.//span[text()='Comprar']]")
        
        time.sleep(0.5)
        button.click()
        log("OK", "Botón 'Comprar' clickeado")
        time.sleep(8)  # Esperar a que carguen los asientos
        return True, None
    except Exception as e:
        return False, e


def go_back_from_seats(driver) -> tuple[bool, str | Exception | None]:
    """
    Vuelve atrás desde la pantalla de selección de asientos.
    
    Retorna (éxito, detalle_error).
    """
    try:
        driver.back()
        time.sleep(1)
        log("OK", "Navegador retrocedió a la lista de servicios")
        return True, None
    except Exception as e:
        return False, e


# ---------------------------------------------------------------------------
# Extracción de horarios desde el HTML
# ---------------------------------------------------------------------------

def extract_times_from_html(html: str) -> list[tuple]:
    """
    Analiza el HTML renderizado y extrae las horas de salida y precios de cada servicio de bus.

    Estructura HTML esperada en Turbus.cl:
      <div class="service-item_service_item__1JAq8">        ← contenedor de un bus
        <div class="service-item_date_time_wrapper_inner__Jjbq0">  ← bloque origen (salida)
          <div>Martes 17</div>
          <div>08:30</div>      ← hora de salida (último <div> del bloque)
        </div>
        <div class="service-item_seats_price_wrapper__UuW7A">  ← bloque precio
          <span> $4.500 </span>  ← precio
        </div>
      </div>

    Retorna lista de tuplas (hora, precio) donde:
      - hora es string en formato "HH:MM"
      - precio es string con el valor (ej. "$4.500")
    """
    log("STEP", "Analizando HTML con BeautifulSoup...")
    soup = BeautifulSoup(html, "html.parser")

    # Busca todos los contenedores de servicios de bus
    service_items = soup.find_all(class_="service-item_service_item__1JAq8")
    total_found = len(service_items)
    log("INFO", f"Se encontraron {total_found} bloques de servicio en el HTML")

    if total_found == 0:
        log("WARN", "No se encontró ningún bloque de servicio. "
            "Posibles causas: la clase CSS cambió, la página no cargó, o el JS no terminó.")

    services_found = []

    for idx, item in enumerate(service_items, start=1):
        service_label = f"Servicio {idx}/{total_found}"

        # Dentro de cada servicio hay (al menos) dos bloques de fecha/hora:
        # inners[0] = ORIGEN (ciudad de salida, hora de salida)
        # inners[1] = DESTINO (ciudad de llegada, hora de llegada)
        inners = item.find_all(class_="service-item_date_time_wrapper_inner__Jjbq0")

        if not inners:
            # Este bloque no tiene la estructura esperada; puede ser un banner o separador
            log("INFO", f"  {service_label}: no tiene bloque de fecha/hora → se omite")
            continue

        # Tomamos solo el primer bloque (origen = salida)
        origin_block = inners[0]

        # Extraemos el texto de todos los <div> no vacíos dentro del bloque de origen
        # Normalmente el último contiene la hora (ej. "08:30") y el anterior la fecha (ej. "Martes 17")
        parts = [d.get_text(strip=True) for d in origin_block.find_all("div") if d.get_text(strip=True)]

        if not parts:
            log("INFO", f"  {service_label}: bloque de origen está vacío → se omite")
            continue

        # El último elemento de la lista es la hora de salida
        time_text = parts[-1]
        log("INFO", f"  {service_label}: texto de hora detectado → '{time_text}'")

        # Intentar extraer HH:MM (con AM/PM opcional) del texto
        match = TIME_RE.search(time_text)
        if not match:
            log("INFO", f"  {service_label}: no se encontró patrón HH:MM en '{time_text}' → se omite")
            continue

        hh   = int(match.group(1))   # horas
        mm   = int(match.group(2))   # minutos
        ampm = match.group(3)        # sufijo AM/PM (puede ser None)

        # Validación básica de rangos
        if not (0 <= hh < 24 and 0 <= mm < 60):
            log("WARN", f"  {service_label}: valores fuera de rango (hh={hh}, mm={mm}) → se omite")
            continue

        # --- Normalización AM/PM → 24 horas ---
        # Solo aplica si el texto incluye sufijo y la hora está en rango 1–12
        if ampm:
            suffix = ampm.strip().upper()
            if 1 <= hh <= 12:
                if suffix == "AM" and hh == 12:
                    # 12:XX AM = medianoche → 00:XX
                    hh = 0
                elif suffix == "PM" and hh != 12:
                    # 1:XX PM → 13:XX, 11:XX PM → 23:XX, etc.
                    hh += 12
            # Si la hora ya viene >12 con PM (ej. "21:30 PM"), ignoramos el sufijo
            # ya que la hora ya está en 24h

        # Formateamos como "HH:MM" con cero a la izquierda
        normalized_time = f"{hh:02d}:{mm:02d}"
        log("OK", f"  {service_label}: hora de salida extraída → {normalized_time}")

        # --- Extracción del precio ---
        price_text = "N/A"
        price_wrapper = item.find(class_="service-item_seats_price_wrapper__UuW7A")
        if price_wrapper:
            # Buscar el último <span> que contenga el precio
            spans = price_wrapper.find_all("span")
            if spans:
                # Tomar el último span que tiene el valor del precio
                price_text = spans[-1].get_text(strip=True)
                log("OK", f"  {service_label}: precio extraído → {price_text}")
            else:
                log("WARN", f"  {service_label}: no se encontró <span> en el bloque de precio")
        else:
            log("WARN", f"  {service_label}: no se encontró bloque de precio")

        # Guardar tupla (hora, precio)
        services_found.append((normalized_time, price_text))

    # --- Deduplicación y orden ---
    # Usamos un dict para eliminar duplicados por hora, manteniendo el primer precio encontrado
    seen_times = {}
    for hora, precio in services_found:
        if hora not in seen_times:
            seen_times[hora] = precio

    duplicates_removed = len(services_found) - len(seen_times)
    if duplicates_removed > 0:
        log("INFO", f"Se eliminaron {duplicates_removed} horario(s) duplicado(s)")

    # Ordena cronológicamente convirtiendo HH:MM a minutos desde medianoche
    sorted_services = sorted(seen_times.items(), key=lambda x: int(x[0][:2]) * 60 + int(x[0][3:]))

    return sorted_services


# ---------------------------------------------------------------------------
# Punto de entrada principal
# ---------------------------------------------------------------------------

def main():
    # Construir la URL default con la fecha de hoy en formato DD-MM-YYYY
    today2 = '19-06-2026'
    today = date.today().strftime("%d-%m-%Y")
    default_url = f"https://www.turbus.cl/es/pasajes-bus/vi%C3%B1a-del-mar,-chile/santiago,-chile?date_onward={today2}"
    
    parser = argparse.ArgumentParser(
        description="Extrae horarios de buses Turbus (Viña del Mar → Santiago) y los muestra en consola.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
    Ejemplos:
    python scraper_turbus.py
    python scraper_turbus.py --wait 15
    python scraper_turbus.py --no-headless
    python scraper_turbus.py --selenium-url http://localhost:4444/wd/hub
        """
    )
    parser.add_argument(
        "--url", "-u",
        default=default_url,
        help="URL completa de búsqueda en Turbus.cl (default: Viña del Mar → Santiago, hoy)"
    )
    parser.add_argument(
        "--wait", "-w",
        type=int, default=8,
        help="Segundos extra para esperar el renderizado dinámico de JS (default: 8)"
    )
    parser.add_argument(
        "--no-headless",
        dest="headless", action="store_false",
        help="Abre Chrome con interfaz gráfica (útil para depuración)"
    )
    parser.add_argument(
        "--selenium-url",
        dest="selenium_url",
        help="URL de un servidor Selenium remoto (ej. http://localhost:4444/wd/hub). "
             "También acepta la variable de entorno SELENIUM_URL."
    )

    args = parser.parse_args()

    # Encabezado de ejecución
    print("=" * 60)
    print("  SCRAPER DE HORARIOS TURBUS")
    print(f"  Inicio: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)
    log("INFO", f"URL objetivo: {args.url}")
    log("INFO", f"Espera JS:    {args.wait}s")
    log("INFO", f"Headless:     {args.headless}")

    # Determinar URL de Selenium: parámetro CLI tiene prioridad sobre variable de entorno
    selenium_url = args.selenium_url or os.environ.get("SELENIUM_URL")
    if selenium_url:
        log("INFO", f"Selenium:     remoto ({selenium_url})")
    else:
        log("INFO", "Selenium:     local (chromedriver_autoinstaller)")

    # --- Paso 1: Abrir navegador y cargar página ---
    print()
    log("STEP", "=== FASE 1: Abriendo navegador ===")
    driver = None
    try:
        driver = open_browser(args.url, wait_seconds=args.wait, headless=args.headless, selenium_url=selenium_url)
    except Exception as e:
        log("ERROR", f"No se pudo abrir el navegador: {e}")
        sys.exit(2)

    # --- Paso 2: Extraer los horarios y precios del HTML ---
    print()
    log("STEP", "=== FASE 2: Extracción de horarios y precios ===")
    try:
        html = driver.page_source
        services = extract_times_from_html(html)
    except Exception as e:
        log("ERROR", f"Error extrayendo servicios: {e}")
        if driver:
            driver.quit()
        sys.exit(2)

    if not services:
        log("WARN", "No se encontró ningún horario.")
        log("INFO", "Sugerencias:")
        log("INFO", "  → Intenta aumentar --wait (ej. --wait 15) para dar más tiempo al JS")
        log("INFO", "  → Usa --no-headless para ver qué muestra el navegador")
        log("INFO", "  → Verifica que la URL sea válida y tenga una fecha correcta")
        if driver:
            driver.quit()
        sys.exit(1)

    # --- Paso 3: Extraer asientos disponibles para cada bus ---
    print()
    log("STEP", "=== FASE 3: Extracción de asientos disponibles ===")
    
    services_with_seats = []
    for idx, (hora, precio) in enumerate(services, start=1):
        service_label = f"Bus {idx}/{len(services)} ({hora})"
        log("INFO", f"Procesando {service_label}...")
        
        # Hacer click en el botón Comprar
        clicked, click_error = click_purchase_button(driver)
        if not clicked:
            abort(
                f"{service_label}: no se pudo abrir la pantalla de asientos. Proceso detenido.",
                driver=driver,
                cause=click_error,
            )
        
        # Extraer asientos disponibles
        available_seats, layout_found, seats_error = extract_available_seats(driver)
        if not layout_found:
            abort(
                f"{service_label}: no se pudo extraer el mapa de asientos. Proceso detenido.",
                driver=driver,
                cause=seats_error,
            )

        asientos_msg = ", ".join(available_seats) if available_seats else "(ninguno libre)"
        log("OK", f"  {service_label}: {len(available_seats)} asiento(s) disponible(s): {asientos_msg}")
        services_with_seats.append((hora, precio, available_seats))
        
        # Volver a la lista de servicios
        went_back, back_error = go_back_from_seats(driver)
        if not went_back:
            abort(
                f"{service_label}: no se pudo volver a la lista de servicios. Proceso detenido.",
                driver=driver,
                cause=back_error,
            )
        
        time.sleep(1)  # Pausa entre buses para no sobrecargar

    # Cerrar navegador
    if driver:
        driver.quit()

    # --- Resultado final ---
    print()
    print("=" * 90)
    print("  RESULTADO FINAL")
    print("=" * 90)

    log("OK", f"Se encontraron {len(services_with_seats)} horarios disponibles:")
    print()
    for i, (hora, precio, asientos) in enumerate(services_with_seats, start=1):
        asientos_str = ", ".join(asientos) if asientos else "Sin asientos disponibles"
        print(f"  {i:2d}. {hora}  →  {precio}  →  Asientos: {asientos_str}")

    print()
    first_time, first_price, _ = services_with_seats[0]
    last_time, last_price, _ = services_with_seats[-1]
    log("INFO", f"Primer bus: {first_time} ({first_price})  |  Último bus: {last_time} ({last_price})")
    log("INFO", f"Total de salidas: {len(services_with_seats)}")
    
    # Calcular precio mínimo y máximo
    prices = [precio for _, precio, _ in services_with_seats]
    prices_numeric = [parse_price(p) for p in prices]
    
    # Encontrar índices de mínimo y máximo
    if prices_numeric:
        min_price_value = min(prices_numeric)
        max_price_value = max(prices_numeric)
        
        # Encontrar el precio original (con formato) correspondiente
        min_price_idx = prices_numeric.index(min_price_value)
        max_price_idx = prices_numeric.index(max_price_value)
        
        min_price_str = prices[min_price_idx]
        max_price_str = prices[max_price_idx]
        
        log("INFO", f"Precio mínimo: {min_price_str}")
        log("INFO", f"Precio máximo: {max_price_str}")
    
    print("=" * 90)


if __name__ == "__main__":
    main()