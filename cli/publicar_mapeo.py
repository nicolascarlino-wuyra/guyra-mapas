#!/usr/bin/env python3
"""
publicar_mapeo.py — Automatiza la entrega de un mapeo de Guyra Agro a un cliente.

Dado un archivo GeoTIFF (o COG ya generado), este script:
  1. Verifica que sea un Cloud Optimized GeoTIFF válido; si no, lo convierte.
  2. Lo commitea a docs/mapas/<tag>/ del repo del visor y lo pushea a GitHub,
     para que quede servido desde el MISMO origen que el visor (GitHub
     Pages). Importante: NO se usa GitHub Releases, porque esos assets se
     sirven sin cabecera Access-Control-Allow-Origin y el navegador bloquea
     la lectura por CORS (se detectó probando con un archivo real).
  3. Calcula superficie, resolución y genera una miniatura.
  4. Arma el link al visor de Guyra Agro (docs/index.html) con los datos
     del trabajo cargados.
  5. Genera un PDF de una página con esos datos + un código QR al mapa.

Requiere en la máquina donde se ejecuta:
  - Python 3.9+ con: rasterio, rio-cogeo, fpdf2, pyproj, qrcode[pil]
    (instalar con: pip install rasterio rio-cogeo fpdf2 pyproj "qrcode[pil]")
  - GDAL (viene con rasterio, no hace falta instalarlo aparte)
  - git configurado para pushear al repo del visor sin pedir contraseña
    (por ejemplo, con `gh auth login` + `gh auth setup-git`)

Ver config.json para configurar la URL del visor (una sola vez).

Uso:
  python3 publicar_mapeo.py archivo.tif \
      --cliente "Estancia La Ejemplo" \
      --trabajo "Lote 12" \
      --tipo rgb \
      --fecha 2026-09-12
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import unicodedata
from datetime import date
from pathlib import Path
from urllib.parse import urlencode

try:
    import rasterio
    from rasterio.warp import transform_bounds
    from rio_cogeo.cogeo import cog_translate, cog_validate
    from rio_cogeo.profiles import cog_profiles
    from pyproj import Geod
    from fpdf import FPDF
    from fpdf.enums import XPos, YPos
    import qrcode
    import numpy as np
    from PIL import Image
except ImportError as e:
    print(f"Falta instalar una dependencia: {e}")
    print("Corré: pip install rasterio rio-cogeo fpdf2 pyproj \"qrcode[pil]\" pillow numpy")
    sys.exit(1)

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.json"

TIPO_LABELS = {
    "rgb": "Ortomosaico RGB",
    "ndvi": "Índice NDVI (vigor vegetativo)",
    "ndre": "Índice NDRE",
    "ndwi": "Índice NDWI (estrés hídrico)",
    "dsm": "Modelo de elevación (DSM)",
}


def cargar_config():
    if not CONFIG_PATH.exists():
        print(f"No encontré {CONFIG_PATH}.")
        print("Copiá config.example.json a config.json y completá:")
        print('  - "viewer_base_url": la URL pública de GitHub Pages (ej: https://usuario.github.io/repo/)')
        sys.exit(1)
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def slugify(texto: str) -> str:
    texto = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode("ascii")
    texto = re.sub(r"[^\w\s-]", "", texto).strip().lower()
    return re.sub(r"[-\s]+", "-", texto)


def verificar_gh_disponible():
    if shutil.which("gh") is None:
        print("No encontré el comando `gh` (GitHub CLI). Instalalo desde https://cli.github.com/")
        sys.exit(1)
    r = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True)
    if r.returncode != 0:
        print("El GitHub CLI no está autenticado en esta máquina.")
        print("Corré: gh auth login")
        sys.exit(1)


CALIDAD_WEBP = {
    "media": 85,
    "alta": 90,
    "maxima": 95,
}


def asegurar_cog(origen: Path, salida: Path, tipo: str, lossless: bool = False, calidad: str = "alta") -> Path:
    """Prepara el COG a subir.

    Para 'rgb' (salvo --lossless), siempre recomprime, con pérdida, para
    achicar el archivo. Para índices/DSM nunca se recomprime con pérdida
    (arruinaría los valores que el visor necesita para la rampa de color):
    se deja el archivo tal cual si ya es un COG válido, o se convierte sin
    pérdida (deflate) si no lo es.

    IMPORTANTE (bug real, detectado 2026-09-14 con una captura de Nico):
    la primera versión de esto usaba JPEG (compress='jpeg'). GDAL comprime
    JPEG-en-GeoTIFF internamente en espacio de color YCbCr (photometric=
    YCbCr) y reconvierte a RGB al leer -- eso funciona perfecto server-side
    (rasterio/gdalinfo, y por eso el PSNR medido daba bien), pero la
    librería que usa el visor en el navegador (geotiff.js) NO hace esa
    conversión YCbCr->RGB al decodificar: el resultado se ve con un
    corrimiento de color magenta/cian bien visible (confirmado
    reproduciendo el mismo archivo con Playwright: server-side perfecto,
    en el visor real distorsionado). No es un problema de "calidad", es
    un bug de decodificación -- ningún jpeg_quality lo arregla.
    Cambiado a WEBP (compress='webp', que además no usa YCbCr en este
    pipeline): confirmado con Playwright que decodifica bien en el visor
    real, y de yapa da archivos más chicos que JPEG a igual nivel.
    La clave de GDAL para el nivel de compresión WEBP es 'webp_level'
    (0-100, no 'quality': ese nombre genérico se ignora en silencio, el
    mismo tipo de bug que ya hubo con JPEG/jpeg_quality).
    """
    if tipo == "rgb" and not lossless:
        webp_level = CALIDAD_WEBP.get(calidad, CALIDAD_WEBP["alta"])
        print(f"Generando COG optimizado (WEBP, calidad {calidad}={webp_level}) para '{origen.name}'...")
        with rasterio.open(origen) as src:
            profile = cog_profiles.get("webp")
            profile.update(webp_level=webp_level)
            indexes = (1, 2, 3) if src.count >= 3 else None
            cog_translate(
                src, str(salida), profile,
                indexes=indexes, in_memory=False, quiet=False, add_mask=True,
            )
        ok, errores, _ = cog_validate(str(salida))
        if not ok:
            print(f"No se pudo generar un COG válido: {errores}")
            sys.exit(1)
        origen_mb = origen.stat().st_size / 1e6
        salida_mb = salida.stat().st_size / 1e6
        print(f"Tamaño: {origen_mb:.1f} MB -> {salida_mb:.1f} MB (para subir sin pérdida usá --lossless)")
        return salida

    ok, errores, _ = cog_validate(str(origen))
    if ok:
        print(f"'{origen.name}' ya es un COG válido, no hace falta convertir.")
        return origen

    print(f"'{origen.name}' no es un COG válido ({errores}). Convirtiendo (sin pérdida)...")
    with rasterio.open(origen) as src:
        profile = cog_profiles.get("deflate")
        cog_translate(src, str(salida), profile, in_memory=False, quiet=False)
    ok, errores, _ = cog_validate(str(salida))
    if not ok:
        print(f"No se pudo generar un COG válido: {errores}")
        sys.exit(1)
    print(f"COG generado en: {salida}")
    return salida


def calcular_metadatos(cog_path: Path):
    with rasterio.open(cog_path) as src:
        bounds_wgs84 = transform_bounds(src.crs, "EPSG:4326", *src.bounds)
        left, bottom, right, top = bounds_wgs84
        geod = Geod(ellps="WGS84")
        area_m2, _ = geod.polygon_area_perimeter(
            [left, right, right, left], [bottom, bottom, top, top]
        )
        area_ha = abs(area_m2) / 10000.0

        # resolución aproximada en cm/pixel (usando el ancho en x del pixel original)
        px_size_x = abs(src.transform.a)
        if src.crs and src.crs.is_geographic:
            # convertir grados a metros aprox en el centro de la escena
            lat_centro = (bottom + top) / 2
            metros_por_grado = 111320 * abs(np.cos(np.radians(lat_centro)))
            res_cm = px_size_x * metros_por_grado * 100
        else:
            res_cm = px_size_x * 100

        return {
            "bounds_wgs84": (left, bottom, right, top),
            "area_ha": round(area_ha, 2),
            "resolucion_cm": round(res_cm, 1),
            "ancho": src.width,
            "alto": src.height,
            "bandas": src.count,
        }


def generar_miniatura(cog_path: Path, salida_png: Path, tipo: str, max_dim=700):
    with rasterio.open(cog_path) as src:
        escala = max(src.width, src.height) / max_dim
        out_w = max(1, int(src.width / escala))
        out_h = max(1, int(src.height / escala))

        if tipo == "rgb" and src.count >= 3:
            data = src.read([1, 2, 3], out_shape=(3, out_h, out_w))
            arr = np.transpose(data, (1, 2, 0))
            if arr.dtype != np.uint8:
                arr = np.clip(arr / arr.max() * 255, 0, 255).astype(np.uint8) if arr.max() > 0 else arr.astype(np.uint8)
            img = Image.fromarray(arr, mode="RGB")
        else:
            data = src.read(1, out_shape=(out_h, out_w)).astype(np.float32)
            data_norm = data - np.nanmin(data)
            maxv = np.nanmax(data_norm) or 1
            data_norm = (data_norm / maxv * 255).astype(np.uint8)
            img = Image.fromarray(data_norm, mode="L").convert("RGB")

        img.save(salida_png)


def publicar_en_pages(archivo: Path, tag: str) -> str:
    """Copia el archivo a docs/mapas/<tag>/ del repo y lo pushea a GitHub.

    Importante: el archivo tiene que quedar en el MISMO origen que el visor
    (GitHub Pages), no en un GitHub Release. Los Releases se sirven desde
    release-assets.githubusercontent.com sin cabecera Access-Control-Allow-Origin,
    así que el navegador bloquea la lectura por CORS sin importar la conexión
    (esto se detectó probando con un archivo real: quedaba cargando para
    siempre en cualquier dispositivo). Sirviendo el archivo desde docs/ del
    mismo repo que el visor, es same-origin y el problema desaparece.
    """
    repo_root = SCRIPT_DIR.parent
    destino_dir = repo_root / "docs" / "mapas" / tag
    destino_dir.mkdir(parents=True, exist_ok=True)
    destino = destino_dir / archivo.name
    shutil.copy2(archivo, destino)

    print(f"Subiendo '{archivo.name}' a docs/mapas/{tag}/ (mismo origen que el visor)...")
    subprocess.run(["git", "add", str(destino)], cwd=repo_root, check=True)
    commit = subprocess.run(
        ["git", "-c", "user.email=nicolascarlino@gmail.com", "-c", "user.name=Nicolas Carlino",
         "commit", "-q", "-m", f"Mapeo: {tag}"],
        cwd=repo_root, capture_output=True, text=True,
    )
    if commit.returncode != 0 and "nothing to commit" not in (commit.stdout + commit.stderr):
        print("Error al commitear:", commit.stderr)
        sys.exit(1)
    push = subprocess.run(["git", "push", "origin", "main"], cwd=repo_root, capture_output=True, text=True)
    if push.returncode != 0:
        print("Error al subir a GitHub:", push.stderr)
        sys.exit(1)

    return f"mapas/{tag}/{archivo.name}"


def armar_link_visor(viewer_base_url: str, cog_url: str, cliente: str, trabajo: str,
                      fecha: str, tipo: str, nota: str) -> str:
    params = {"url": cog_url, "cliente": cliente, "trabajo": trabajo, "fecha": fecha, "tipo": tipo}
    if nota:
        params["nota"] = nota
    base = viewer_base_url.rstrip("/")
    sep = "" if base.endswith(".html") else "/"
    return f"{base}{sep}?{urlencode(params)}"


def generar_qr(link: str, salida_png: Path):
    img = qrcode.make(link)
    img.save(salida_png)


def generar_pdf(salida_pdf: Path, cliente: str, trabajo: str, fecha: str, tipo: str,
                 nota: str, metadatos: dict, thumbnail_png: Path, qr_png: Path, link: str):
    pdf = FPDF(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=False)
    pdf.add_page()

    pdf.set_fill_color(27, 94, 32)  # verde Guyra Agro
    pdf.rect(0, 0, 210, 22, style="F")
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 16)
    pdf.set_xy(10, 6)
    pdf.cell(0, 10, "Guyra Agro - Reporte de mapeo")

    pdf.set_text_color(20, 20, 20)
    pdf.set_xy(10, 30)
    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 8, cliente, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_x(10)
    pdf.set_font("Helvetica", "", 11)
    pdf.cell(0, 7, f"{trabajo}  ·  {fecha}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_x(10)
    pdf.cell(0, 7, TIPO_LABELS.get(tipo, tipo), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    if nota:
        pdf.set_x(10)
        pdf.multi_cell(190, 6, nota)

    pdf.ln(3)
    pdf.set_x(10)
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 6, f"Superficie relevada: {metadatos['area_ha']} ha", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_x(10)
    pdf.cell(0, 6, f"Resolucion aproximada: {metadatos['resolucion_cm']} cm/pixel", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    img_y = pdf.get_y() + 4
    pdf.image(str(thumbnail_png), x=10, y=img_y, w=120)
    pdf.image(str(qr_png), x=140, y=img_y, w=55)
    pdf.set_xy(140, img_y + 57)
    pdf.set_font("Helvetica", "I", 8)
    pdf.multi_cell(55, 4, "Escaneá el código para ver el mapa interactivo", align="C")

    pdf.set_y(-20)
    pdf.set_font("Helvetica", "", 8)
    pdf.set_text_color(90, 90, 90)
    pdf.multi_cell(0, 5, f"Link directo: {link}", align="C")

    pdf.output(str(salida_pdf))


def main():
    ap = argparse.ArgumentParser(description="Publica un mapeo de Guyra Agro para un cliente.")
    ap.add_argument("archivo", type=Path, help="Ruta al GeoTIFF/COG a publicar")
    ap.add_argument("--cliente", required=True)
    ap.add_argument("--trabajo", required=True)
    ap.add_argument("--tipo", choices=TIPO_LABELS.keys(), default="rgb")
    ap.add_argument("--fecha", default=date.today().isoformat())
    ap.add_argument("--nota", default="")
    ap.add_argument("--salida", type=Path, default=Path("salida"), help="Carpeta donde dejar COG/PDF generados")
    ap.add_argument("--sin-subir", action="store_true",
                     help="No sube nada a GitHub; solo procesa el archivo localmente (para pruebas)")
    ap.add_argument("--lossless", action="store_true",
                     help="Para --tipo rgb: no recomprimir, subir sin pérdida (archivo mucho más pesado)")
    ap.add_argument("--calidad", choices=CALIDAD_WEBP.keys(), default="alta",
                     help="Para --tipo rgb: nivel de compresion WEBP. media=mas liviano/mas rapido de subir, "
                          "alta=equilibrio (default), maxima=mejor calidad/archivo mas pesado")
    args = ap.parse_args()

    if not args.archivo.exists():
        print(f"No encontré el archivo: {args.archivo}")
        sys.exit(1)

    args.salida.mkdir(parents=True, exist_ok=True)
    slug_cliente = slugify(args.cliente)
    slug_trabajo = slugify(args.trabajo)
    tag = f"{slug_cliente}-{slug_trabajo}-{args.fecha}"

    cog_final = asegurar_cog(args.archivo, args.salida / f"{tag}_{args.tipo}.tif", args.tipo, args.lossless, args.calidad)

    metadatos = calcular_metadatos(cog_final)
    print(f"Superficie: {metadatos['area_ha']} ha | Resolución: {metadatos['resolucion_cm']} cm/px")

    miniatura = args.salida / f"{tag}_miniatura.png"
    generar_miniatura(cog_final, miniatura, args.tipo)

    config = {} if args.sin_subir else cargar_config()

    viewer_base_url = config.get("viewer_base_url", "http://localhost:8792/index.html")

    if args.sin_subir:
        cog_url = f"file://{cog_final.resolve()}"
        print("Modo --sin-subir: no se sube nada a GitHub, se usa una URL local de prueba.")
    else:
        ruta_relativa = publicar_en_pages(cog_final, tag)
        cog_url = f"{viewer_base_url.rstrip('/')}/{ruta_relativa}"

    link = armar_link_visor(viewer_base_url, cog_url, args.cliente, args.trabajo,
                             args.fecha, args.tipo, args.nota)

    qr_png = args.salida / f"{tag}_qr.png"
    generar_qr(link, qr_png)

    pdf_path = args.salida / f"{tag}_reporte.pdf"
    generar_pdf(pdf_path, args.cliente, args.trabajo, args.fecha, args.tipo,
                args.nota, metadatos, miniatura, qr_png, link)

    print("\nListo.")
    print(f"Link para el cliente: {link}")
    print(f"PDF generado en: {pdf_path.resolve()}")


if __name__ == "__main__":
    main()
